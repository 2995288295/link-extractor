"""链接提取核心模块：抖音/小红书 链接 → 提取信息 + 转换链接

提炼自 social-media-toolkit SDK（开源）与 video-tools-local 项目：
- 抖音：移动端 UA 请求分享页，解析 window._ROUTER_DATA JSON（纯 requests，无签名逆向）
- 小红书：优先解析 __INITIAL_STATE__ 页面状态；失败用 meta 标签轻量兜底
- 转换链接：抖音 → douyin.com/video/{id}（去渠道参数）；小红书 → explore/{id} + 保留 xsec_token

依赖仅 requests，轻量无浏览器，适合低内存服务器部署。
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 连接池

_session_local = threading.local()


def _new_session() -> requests.Session:
    """创建独立会话；风控重试不能复用原会话的 Cookie。"""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_session() -> requests.Session:
    """每线程一个 Session（连接池复用），线程安全。"""
    if not hasattr(_session_local, "session"):
        _session_local.session = _new_session()
    return _session_local.session


# ---------------------------------------------------------------- 结果缓存

_result_cache: dict[str, tuple[dict[str, Any], float]] = {}
CACHE_TTL_SECONDS = 24 * 60 * 60      # 24 小时
CACHE_MAX_ENTRIES = 500
_CACHE_DB = Path(__file__).resolve().parents[1] / "data" / "history.db"


def _cache_key(url: str) -> str:
    """按完整输入链接隔离缓存，避免跨用户复用带访问参数的结果。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _public_work_cache_key(platform: str, post_id: str) -> str:
    """为公开且稳定的抖音作品生成跨分享形式的缓存键。"""
    return hashlib.sha256(f"public-work:{platform}:{post_id}".encode("utf-8")).hexdigest()


def _extract_post_id_from_url(url: str) -> str:
    """从 URL 预提取作品 ID（不发请求）：抖音 video/note/数字ID；小红书 explore/discovery/item/ID。"""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    if "douyin" in url.lower():
        m = re.search(r"/(?:video|note|share/(?:video|slides|note))/(\d+)", path, re.I)
        if m:
            return m.group(1)
        query = parse_qs(urlparse(url).query)
        for key in ("modal_id", "aweme_id", "item_id"):
            value = (query.get(key) or [""])[0]
            if re.fullmatch(r"\d{8,}", value):
                return value
    if "xiaohongshu" in url or "xhslink" in url:
        m = re.search(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)", path)
        return m.group(1) if m else ""
    return ""


def cache_get(key: str):
    """先查本进程热缓存，再查 SQLite 共享缓存。"""
    if not key:
        return None
    entry = _result_cache.get(key)
    if entry:
        data, ts = entry
        if time.time() - ts <= CACHE_TTL_SECONDS:
            return data
        _result_cache.pop(key, None)
    try:
        conn = sqlite3.connect(_CACHE_DB, timeout=2)
        row = conn.execute(
            "SELECT result_json FROM extract_cache WHERE cache_key = ? AND expires_at > ?",
            (key, time.time()),
        ).fetchone()
        conn.close()
        if row:
            data = json.loads(row[0])
            _result_cache[key] = (data, time.time())
            return data
    except (OSError, sqlite3.Error, ValueError):
        pass
    return None


def cache_put(key: str, data: dict[str, Any]) -> None:
    """写入进程热缓存及 SQLite 共享缓存；共享缓存跨 worker 和重启有效。"""
    if not key:
        return
    now = time.time()
    _result_cache[key] = (data, now)
    if len(_result_cache) > CACHE_MAX_ENTRIES:
        items = sorted(_result_cache.items(), key=lambda kv: kv[1][1])
        for k, _v in items[: len(items) // 2]:
            _result_cache.pop(k, None)
    try:
        conn = sqlite3.connect(_CACHE_DB, timeout=2)
        conn.execute(
            "INSERT OR REPLACE INTO extract_cache (cache_key, result_json, expires_at, updated_at) VALUES (?,?,?,?)",
            (key, json.dumps(data, ensure_ascii=False), now + CACHE_TTL_SECONDS, now),
        )
        conn.commit()
        conn.close()
    except (OSError, sqlite3.Error):
        pass

# ---------------------------------------------------------------- 安全校验

ALLOWED_DOMAINS = {
    "v.douyin.com", "www.douyin.com", "douyin.com", "douyinvideo.com",
    "www.iesdouyin.com", "iesdouyin.com",
    "www.xiaohongshu.com", "xiaohongshu.com",
    "xhslink.com", "xhslink.cn",
}

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DOUYIN_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

DOUYIN_RETRY_MODE = os.environ.get("DOUYIN_RETRY_MODE", "adaptive").strip().lower()
DOUYIN_ADAPTIVE_DELAY_MIN = 0.4
DOUYIN_ADAPTIVE_DELAY_MAX = 0.8

XHS_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.xiaohongshu.com/",
}

# ---------------------------------------------------------------- 平台请求闸门
# 月底创作者高峰多人同时批量提取时，多 worker 会瞬时打出大量平台请求，
# 极易触发抖音/小红书风控。全局闸门限制每个平台的并发数与最小请求间隔：
# - 抖音：默认最多 2 个并发请求、间隔 ≥ 0.8s（此前抖音无全局节流，本次新增）
# - 小红书：串行 + 间隔 ≥ 1.0s（原 0.25s 对数据中心 IP 偏高，调大降低触发概率）
# 均可用环境变量覆盖，无需改代码即可调参。

def _float_env(name: str, default: float, lo: float = 0.0, hi: float = 60.0) -> float:
    try:
        val = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        val = default
    return min(hi, max(lo, val))


class _PlatformGate:
    """平台请求闸门：限制全局并发数与最小请求间隔（线程安全）。

    多个 worker 线程（批量提取/多用户并发）共享同一闸门，
    超出并发上限的请求阻塞排队，避免瞬时请求洪峰触发平台风控。
    """

    def __init__(self, name: str, max_concurrency: int, min_interval: float):
        self.name = name
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._min_interval = max(0.0, min_interval)

    def acquire(self) -> None:
        """获取许可：并发超限时排队等待；同时保证与上一次请求的最小间隔。"""
        self._sem.acquire()
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._min_interval
        if wait > 0:
            time.sleep(wait)

    def release(self) -> None:
        self._sem.release()


DOUYIN_GATE = _PlatformGate(
    "douyin",
    max_concurrency=int(os.environ.get("DOUYIN_GATE_CONCURRENCY", "2")),
    min_interval=_float_env("DOUYIN_GATE_INTERVAL", 0.8),
)

# 每个 Gunicorn worker 内串行化小红书页面请求，并保留很短的自然间隔。
# 批量任务此前只有通用抖动，多个线程仍可能同时打到小红书而触发临时验证页。
_xhs_request_slot = threading.BoundedSemaphore(1)
_xhs_schedule_lock = threading.Lock()
_xhs_next_request_at = 0.0
XHS_MIN_REQUEST_GAP_SECONDS = _float_env("XHS_MIN_REQUEST_GAP_SECONDS", 1.0)

_dns_cache: dict[str, tuple[tuple[str, ...], float]] = {}
_dns_cache_lock = threading.Lock()
DNS_CACHE_TTL_SECONDS = 60


def _is_safe_url(url: str) -> bool:
    """校验 URL 属于允许域名，且解析后不指向私有/回环 IP（防 SSRF）。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            return False
        with _dns_cache_lock:
            cached = _dns_cache.get(host)
        if cached and cached[1] > time.monotonic():
            addresses = [(None, None, None, None, (ip, 0)) for ip in cached[0]]
        else:
            addresses = socket.getaddrinfo(host, None)
        if not addresses:
            return False
        for info in addresses:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        with _dns_cache_lock:
            _dns_cache[host] = (tuple(sorted({info[4][0] for info in addresses})), time.monotonic() + DNS_CACHE_TTL_SECONDS)
        return True
    except Exception:
        return False


def _validate_url_integrity(url: str) -> str:
    """预检分享链接是否被聊天工具截断（作品 ID 明显短于正常长度）。

    返回错误文案（正常返回空字符串）。仅校验完整链接路径中的作品 ID：
    - 小红书：/explore/{id} 或 /discovery/item/{id}，标准 24 位十六进制 ID
    - 抖音：/video/{id} 或 /note/{id} 等，标准 19 位数字 ID
    - 短链（v.douyin.com/xhslink）路径不含作品 ID，跳过不校验
    校验不通过时直接拦截返回，避免把残缺链接送到平台请求层反复打接口。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
        path = urlparse(url).path
    except Exception:
        return ""
    if "xiaohongshu" in host:
        m = re.search(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)", path)
        if m and len(m.group(1)) < 20:
            return f"小红书链接不完整（作品 ID 仅 {len(m.group(1))} 位，标准 24 位），可能被聊天工具截断，请从 App 重新复制完整链接"
    elif "douyin" in host:
        m = re.search(r"/(?:video|note|share/(?:video|slides|note))/(\d+)", path, re.I)
        if m and len(m.group(1)) < 15:
            return f"抖音链接不完整（作品 ID 仅 {len(m.group(1))} 位，标准 19 位），可能被聊天工具截断，请从 App 重新复制完整链接"
    return ""


def _safe_get_with_redirects(url: str, *, headers=None, timeout=10, max_redirects=5):
    """逐跳校验重定向目标后再请求，避免先访问内网再做检查。"""
    current = url
    for _ in range(max_redirects + 1):
        if not _is_safe_url(current):
            raise RuntimeError(f"链接域名或网络地址不在允许范围内: {current[:80]}")
        resp = _get_session().get(
            current,
            allow_redirects=False,
            timeout=timeout,
            headers=headers or {},
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("Location", "")
        if not location:
            return resp
        current = urljoin(current, location)
    raise RuntimeError("链接重定向次数过多")


def _safe_follow_redirects(session, url: str, *, headers=None, timeout=30, max_redirects=5):
    """手动跟随重定向（allow_redirects=False），每跳校验目标域名与公网 IP。

    用于抖音/小红书提取链路：入口 URL 已过 _is_safe_url 白名单，
    此处确保重定向目标也安全（防被劫持跳转到内网）。
    返回 (最终响应, 最终URL)；调用方用最终 URL 提取信息。
    """
    current = url
    for _ in range(max_redirects + 1):
        if not _is_safe_url(current):
            raise RuntimeError(f"重定向目标不在允许范围: {current[:80]}")
        resp = session.get(
            current,
            headers=headers or {},
            timeout=timeout,
            allow_redirects=False,
        )
        resp.raise_for_status()
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                return resp, current
            current = urljoin(current, location)
            continue
        return resp, current
    raise RuntimeError("重定向次数过多")


# ---------------------------------------------------------------- 工具函数

def _extract_first_url(text: str) -> str:
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", text)
    if not m:
        raise ValueError("未在输入中找到链接")
    return m.group(0)


def _normalize_media_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        return urlparse._replace(parsed, fragment="").geturl()
    except Exception:
        return url


def _first_media_url(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v:
            return v
    return None


def _first_existing(data: dict, *keys: str) -> Any:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return None


def _extract_xsec_token(url: str) -> Optional[str]:
    query = parse_qs(urlparse(url).query)
    return (query.get("xsec_token") or [None])[0]


def _resolve_short_link(url: str) -> str:
    """解析短链接：xhslink.cn / v.douyin.com → 最终 URL"""
    if "xhslink" in url or "douyin" in url:
        try:
            resp = _safe_get_with_redirects(
                url, timeout=10, headers={"User-Agent": DEFAULT_UA}
            )
            if resp.url and resp.url != url:
                logger.info("短链接已解析: %s → %s", url[:50], resp.url[:80])
                return resp.url
        except Exception as e:
            logger.warning("短链接解析失败: %s", e)
    return url


def _canonicalize_media_url(platform: str, url: str, post_id: str = "") -> str:
    """生成无渠道参数的规范链接。

    抖音 → https://www.douyin.com/video/{id}
    小红书 → https://www.xiaohongshu.com/explore/{id}?保留xsec_token等参数
    """
    platform = str(platform or "").strip().lower()
    source_url = str(url or "").strip()
    work_id = str(post_id or "").strip()
    if not work_id:
        patterns = {
            "douyin": (
                r"/(?:video|note)/(\d+)",
                r"/share/(?:video|slides)/(\d+)",
            ),
            "xiaohongshu": (
                r"/(?:discovery/item|explore)/([0-9A-Za-z]+)",
            ),
        }
        for pattern in patterns.get(platform, ()):
            m = re.search(pattern, urlparse(source_url).path, re.I)
            if m:
                work_id = m.group(1)
                break

    if platform == "douyin" and work_id:
        return f"https://www.douyin.com/video/{work_id}"
    if platform == "xiaohongshu" and work_id:
        # 小红书必须用 /explore/ 路径且保留 xsec_token 才能打开
        parsed_src = urlparse(source_url)
        canonical = f"https://www.xiaohongshu.com/explore/{work_id}"
        if parsed_src.query:
            canonical += f"?{parsed_src.query}"
        return canonical
    try:
        return urlparse._replace(urlparse(source_url), query="", fragment="").geturl()
    except Exception:
        return source_url


# ---------------------------------------------------------------- 结果模型

@dataclass
class ExtractResult:
    """统一的提取结果。"""
    success: bool
    platform: str = ""            # 中文：抖音 / 小红书
    platform_raw: str = ""        # douyin / xiaohongshu
    title: str = ""
    caption: str = ""             # 文案
    author_name: str = ""
    publish_time: str = ""
    like_count: int = 0
    video_url: str = ""           # 原始/提取的视频链接
    canonical_url: str = ""       # 转换后的规范链接（优先展示）
    cover_url: str = ""
    post_id: str = ""             # 作品 ID（缓存键）
    error: str = ""
    hint: str = ""
    partial: bool = False           # 链接已转换，但平台内容字段未完整取到
    cache_hit: bool = False
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可缓存/可序列化的字典。"""
        return {
            "success": self.success,
            "platform": self.platform,
            "platform_raw": self.platform_raw,
            "title": self.title,
            "caption": self.caption,
            "author_name": self.author_name,
            "publish_time": self.publish_time,
            "like_count": self.like_count,
            "video_url": self.video_url,
            "canonical_url": self.canonical_url,
            "cover_url": self.cover_url,
            "post_id": self.post_id,
            "error": self.error,
            "hint": self.hint,
            "partial": self.partial,
            "cache_hit": self.cache_hit,
            "telemetry": self.telemetry,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExtractResult":
        return cls(
            success=d.get("success", False),
            platform=d.get("platform", ""),
            platform_raw=d.get("platform_raw", ""),
            title=d.get("title", ""),
            caption=d.get("caption", ""),
            author_name=d.get("author_name", ""),
            publish_time=d.get("publish_time", ""),
            like_count=d.get("like_count", 0),
            video_url=d.get("video_url", ""),
            canonical_url=d.get("canonical_url", ""),
            cover_url=d.get("cover_url", ""),
            post_id=d.get("post_id", ""),
            error=d.get("error", ""),
            hint=d.get("hint", ""),
            partial=d.get("partial", False),
            cache_hit=d.get("cache_hit", False),
            telemetry=d.get("telemetry", {}) or {},
        )


# ---------------------------------------------------------------- 抖音提取

def _parse_douyin_video_info(html: str) -> Optional[dict[str, Any]]:
    """从抖音 HTML 中提取 videoInfoRes；页面无 _ROUTER_DATA 或结构不完整时返回 None。

    注意：抖音风控时可能返回「有 _ROUTER_DATA 标记但 loaderData 是占位/不完整」的页面，
    因此不能只看正则是否匹配，必须校验能否解析出 videoInfoRes。
    """
    # 抖音页面会在 JSON 结尾附加分号，不能直接把整个 script 内容交给
    # json.loads；raw_decode 可以安全读取首个 JSON 值并忽略尾部脚本字符。
    patterns = (
        r"window\._ROUTER_DATA\s*=\s*",
        r"window\._SSR_DATA\s*=\s*",
    )

    def _walk(value: Any) -> Optional[dict[str, Any]]:
        if isinstance(value, dict):
            candidate = value.get("videoInfoRes")
            if isinstance(candidate, dict) and (candidate.get("item_list") or candidate.get("filter_list")):
                return candidate
            for child in value.values():
                found = _walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = _walk(child)
                if found:
                    return found
        return None

    for prefix in patterns:
        for match in re.finditer(prefix, html, flags=re.DOTALL):
            blob = html[match.end():]
            try:
                data, _end = json.JSONDecoder().raw_decode(blob.lstrip())
            except (ValueError, TypeError):
                continue
            found = _walk(data)
            if found:
                return found
    return None


def _classify_douyin_retry_response(html: str, parsed: Optional[dict[str, Any]]) -> str:
    """给重试结果打轻量标签，便于区分风控、占位页和不可用作品。"""
    if parsed:
        if parsed.get("item_list"):
            return "success"
        if parsed.get("filter_list"):
            return "unavailable_content"
        return "empty_video_info"
    text = (html or "").lower()
    if any(token in text for token in ("验证码", "captcha", "challenge", "sec_verify", "verifycenter")):
        return "challenge_page"
    if "_router_data" in text or "_ssr_data" in text:
        return "placeholder_router_data"
    return "missing_video_info"


def _douyin_target(url: str) -> tuple[str, str]:
    """返回 (作品类型, ID)，并明确区分视频、图集和用户主页。"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    match = re.search(r"/(video|note)/(\d+)$", path, re.I)
    if match:
        return match.group(1).lower(), match.group(2)
    match = re.search(r"/share/(video|slides|note)/(\d+)$", path, re.I)
    if match:
        kind = "note" if match.group(1).lower() in ("slides", "note") else "video"
        return kind, match.group(2)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        value = (query.get(key) or [""])[0]
        if re.fullmatch(r"\d{8,}", value):
            return "video", value
    match = re.search(r"/(?:share/)?user/([^/]+)$", path, re.I)
    if match:
        return "user", unquote(match.group(1))
    for key in ("sec_uid", "sec_user_id"):
        value = (query.get(key) or [""])[0]
        if value:
            return "user", value
    return "unknown", ""


def _extract_douyin(url: str, telemetry: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """抖音提取入口：先过全局请求闸门（限制并发与间隔），再执行真实提取。

    闸门可被环境变量覆盖（DOUYIN_GATE_CONCURRENCY / DOUYIN_GATE_INTERVAL），
    用于月底创作者高峰多人同时批量提取时防止瞬时请求洪峰触发风控。
    """
    DOUYIN_GATE.acquire()
    try:
        return _extract_douyin_locked(url, telemetry)
    finally:
        DOUYIN_GATE.release()


def _extract_douyin_locked(url: str, telemetry: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """抖音提取：移动端分享页 _ROUTER_DATA JSON 解析。

    SDK 原版策略：Session 保持 cookie + 首次响应优先解析；解析不出
    videoInfoRes 时（无 _ROUTER_DATA 或占位页）用构造的 iesdouyin
    share URL 二次请求；仍失败则稍等带新会话重试一次，
    以对抗抖音偶发的 JS 挑战页/占位页风控响应。
    """
    source_url = _extract_first_url(url)
    session = _get_session()

    # 首次请求：手动跟随重定向到最终分享页（每跳校验目标安全）。
    request_started = time.perf_counter()
    share_response, final_url = _safe_follow_redirects(
        session, source_url, headers=DOUYIN_MOBILE_HEADERS, timeout=30
    )
    if telemetry is not None:
        telemetry["initial_request_ms"] = round((time.perf_counter() - request_started) * 1000)
    share_kind, video_id = _douyin_target(final_url)
    if share_kind == "user":
        profile_url = f"https://www.douyin.com/user/{quote(video_id, safe='._-')}"
        return {
            "platform": "douyin",
            "title": "抖音用户主页",
            "caption": "",
            "author_name": "",
            "publish_time": "",
            "like_count": 0,
            "video_url": profile_url,
            "canonical_url": profile_url,
            "cover_url": None,
            "post_id": video_id,
            "partial": True,
            "hint": "链接已转换为抖音用户主页；主页不包含单条作品的文案和数据",
        }
    if not video_id:
        share_kind, video_id = _douyin_target(source_url)
    if share_kind == "user":
        profile_url = f"https://www.douyin.com/user/{quote(video_id, safe='._-')}"
        return {
            "platform": "douyin",
            "title": "抖音用户主页",
            "caption": "",
            "author_name": "",
            "publish_time": "",
            "like_count": 0,
            "video_url": profile_url,
            "canonical_url": profile_url,
            "cover_url": None,
            "post_id": video_id,
            "partial": True,
            "hint": "链接已转换为抖音用户主页；主页不包含单条作品的文案和数据",
        }
    if not video_id:
        raise ValueError("抖音链接未包含可识别的作品 ID，可能是直播、商品或失效链接")

    share_url = f"https://www.iesdouyin.com/share/{share_kind}/{video_id}"
    parse_started = time.perf_counter()
    video_info_res = _parse_douyin_video_info(share_response.text)
    retry_outcomes: list[str] = [_classify_douyin_retry_response(share_response.text, video_info_res)]
    successful_attempt = 1 if video_info_res and video_info_res.get("item_list") else 0
    if telemetry is not None:
        telemetry["initial_parse_ms"] = round((time.perf_counter() - parse_started) * 1000)

    # 首次页面无数据时，用真正的新会话重试，避免沿用原会话中的风控 Cookie。
    fresh_sessions: list[requests.Session] = []
    attempts: list[tuple[requests.Session, str]] = [(session, share_url)]
    for target in (share_url, source_url):
        fresh = _new_session()
        fresh_sessions.append(fresh)
        attempts.append((fresh, target))
    try:
        retry_wait_ms = retry_request_ms = retry_parse_ms = 0
        for attempt_no, (attempt_session, target) in enumerate(attempts):
            if video_info_res:
                break
            if attempt_no:
                wait_started = time.perf_counter()
                if DOUYIN_RETRY_MODE == "legacy":
                    time.sleep(1.2 * attempt_no)
                elif attempt_no >= 2:
                    time.sleep(random.uniform(DOUYIN_ADAPTIVE_DELAY_MIN, DOUYIN_ADAPTIVE_DELAY_MAX))
                retry_wait_ms += round((time.perf_counter() - wait_started) * 1000)
            try:
                request_started = time.perf_counter()
                response, _final = _safe_follow_redirects(
                    attempt_session, target, headers=DOUYIN_MOBILE_HEADERS, timeout=30
                )
                retry_request_ms += round((time.perf_counter() - request_started) * 1000)
                parse_started = time.perf_counter()
                video_info_res = _parse_douyin_video_info(response.text)
                retry_parse_ms += round((time.perf_counter() - parse_started) * 1000)
                outcome = _classify_douyin_retry_response(response.text, video_info_res)
                retry_outcomes.append(outcome)
                if outcome == "success":
                    successful_attempt = attempt_no + 2
                # 作品明确不可用时不再继续请求，重试只对风控/占位页有意义。
                if outcome == "unavailable_content":
                    break
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                retry_outcomes.append("request_error")
                logger.info("抖音第 %d 次请求/响应不可用: %s", attempt_no + 1, exc)
        if telemetry is not None:
            telemetry["retry_count"] = max(0, len(retry_outcomes) - 1)
            telemetry["success_attempt"] = successful_attempt
            telemetry["retry_outcomes"] = retry_outcomes
            telemetry["retry_reason"] = retry_outcomes[0] if retry_outcomes else "unknown"
            telemetry["retry_mode"] = DOUYIN_RETRY_MODE
            telemetry["retry_wait_ms"] = retry_wait_ms
            telemetry["retry_request_ms"] = retry_request_ms
            telemetry["retry_parse_ms"] = retry_parse_ms
    finally:
        for fresh in fresh_sessions:
            fresh.close()

    if not video_info_res:
        raise ValueError("抖音页面触发验证，暂时无法获取完整文案，请稍后重试")

    item_list = video_info_res.get("item_list") or []
    if not item_list:
        filter_entry = next(
            (e for e in (video_info_res.get("filter_list") or []) if isinstance(e, dict)),
            {},
        )
        reason = filter_entry.get("filter_reason") or "not_publicly_available"
        detail = filter_entry.get("detail_msg") or filter_entry.get("notice") or "作品不存在或不可公开访问"
        raise ValueError(f"抖音作品不可用（{reason}）：{detail}")

    item = item_list[0]
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    video_url = None
    if url_list:
        video_url = _normalize_media_url(url_list[0].replace("playwm", "play"))

    image_urls = []
    raw_images = item.get("images") or ((item.get("image_post_info") or {}).get("images")) or []
    for image in raw_images:
        if not isinstance(image, dict):
            continue
        image_url = _first_media_url(
            *((image.get("url_list") or [])),
            *(((image.get("display_image") or {}).get("url_list") or [])),
            *(((image.get("owner_watermark_image") or {}).get("url_list") or [])),
        )
        if image_url and image_url not in image_urls:
            image_urls.append(image_url)

    author = item.get("author") or {}
    statistics = item.get("statistics") or {}
    cover = video.get("cover") or {}
    cover_urls = cover.get("url_list") or []
    duration_ms = video.get("duration")
    duration_sec = None
    if isinstance(duration_ms, (int, float)) and duration_ms:
        duration_sec = int(duration_ms / 1000) if duration_ms > 1000 else int(duration_ms)

    title = (item.get("desc") or "").strip() or f"douyin_{video_id}"
    cover_url = _normalize_media_url(cover_urls[0]) if cover_urls else (image_urls[0] if image_urls else None)

    return {
        "platform": "douyin",
        "title": title,
        "caption": (item.get("desc") or "").strip(),
        "author_name": author.get("nickname") or author.get("unique_id") or "",
        "publish_time": _fmt_ts(item.get("create_time")),
        "like_count": int(statistics.get("digg_count") or 0),
        "video_url": video_url or source_url,
        "cover_url": cover_url,
        "post_id": video_id,
        "duration_sec": duration_sec,
    }


# ---------------------------------------------------------------- 小红书提取

class XhsAccessDeniedError(ValueError):
    """小红书将作品页跳到登录页时使用，属于不可通过重试恢复的失败。"""


def _run_xhs_request(callback, telemetry: dict[str, int]):
    """限制同一 worker 的小红书页面请求节奏，返回回调结果。"""
    global _xhs_next_request_at
    queued_at = time.perf_counter()
    _xhs_request_slot.acquire()
    try:
        with _xhs_schedule_lock:
            wait_seconds = max(0.0, _xhs_next_request_at - time.monotonic())
            _xhs_next_request_at = max(time.monotonic(), _xhs_next_request_at) + XHS_MIN_REQUEST_GAP_SECONDS
        if wait_seconds:
            time.sleep(wait_seconds)
        telemetry["xhs_rate_limit_wait_ms"] = telemetry.get("xhs_rate_limit_wait_ms", 0) + round(
            (time.perf_counter() - queued_at) * 1000
        )
        return callback()
    finally:
        _xhs_request_slot.release()


def _extract_xhs_initial_state(
    url: str, *, session: Optional[requests.Session] = None
) -> dict[str, Any]:
    """小红书提取：__INITIAL_STATE__ 页面状态解析（信息最全）。"""
    source_url = _extract_first_url(url)
    response, final_url = _safe_follow_redirects(
        session or _get_session(), source_url, headers=XHS_HEADERS, timeout=30
    )
    response.raise_for_status()

    if urlparse(final_url).path.rstrip("/") == "/login":
        raise XhsAccessDeniedError(
            "小红书平台暂时限制访问，请稍后重试；若持续失败请从 App 重新复制最新分享链接"
        )

    html = response.text
    state_match = re.search(
        r"window\.__INITIAL_STATE__=(.*?)</script>", html, flags=re.DOTALL
    )
    if not state_match:
        raise ValueError("未找到小红书页面状态数据")
    state_blob = state_match.group(1)
    state_blob = re.sub(r":undefined([,}])", r":null\1", state_blob)
    state = json.loads(state_blob)

    note = None
    note_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if isinstance(note_map, dict) and note_map:
        first_entry = next(iter(note_map.values()))
        if isinstance(first_entry, dict):
            note = first_entry.get("note")
    if not isinstance(note, dict):
        raise ValueError("未找到小红书笔记详情")

    note_id = note.get("noteId") or ""
    if not note_id:
        m = re.search(r"/(?:explore|discovery/item)/([^/?]+)", final_url)
        note_id = m.group(1) if m else ""

    image_urls = []
    cover_url = None
    for image in note.get("imageList") or []:
        if not isinstance(image, dict):
            continue
        image_url = image.get("urlDefault") or image.get("urlPre") or image.get("url")
        if image_url:
            normalized = _normalize_media_url(image_url)
            image_urls.append(normalized)
            if not cover_url:
                cover_url = normalized

    video_url = None
    video = note.get("video") or {}
    stream = ((video.get("media") or {}).get("stream") or {})
    for codec in ("h264", "h265", "av1"):
        candidates = stream.get(codec) or []
        if candidates and isinstance(candidates[0], dict):
            master_url = candidates[0].get("masterUrl")
            if master_url:
                video_url = _normalize_media_url(master_url)
                break

    user = note.get("user") or {}
    interact_info = note.get("interactInfo") or note.get("interact_info") or {}

    return {
        "platform": "xiaohongshu",
        "title": (note.get("title") or "").strip(),
        "caption": (note.get("desc") or "").strip(),
        "author_name": user.get("nickname") or user.get("nickName") or user.get("name") or "",
        "publish_time": _fmt_ts(note.get("time")),
        "like_count": int(_first_existing(interact_info, "likedCount", "liked_count", "likeCount", "like_count") or 0),
        "video_url": video_url or source_url,
        "cover_url": cover_url,
        "post_id": note_id,
        "xsec_token": _extract_xsec_token(response.url) or note.get("xsecToken") or _extract_xsec_token(source_url),
    }


def _extract_xhs_lightweight(
    url: str, *, session: Optional[requests.Session] = None
) -> dict[str, Any]:
    """小红书轻量兜底：meta 标签解析（无 __INITIAL_STATE__ 时使用）。"""
    source_url = _extract_first_url(url)
    resp, final_url = _safe_follow_redirects(
        session or _get_session(), source_url, headers=XHS_HEADERS, timeout=10
    )
    resp.encoding = "utf-8"

    if urlparse(final_url).path.rstrip("/") == "/login":
        raise XhsAccessDeniedError(
            "小红书平台暂时限制访问，请稍后重试；若持续失败请从 App 重新复制最新分享链接"
        )
    if "404" in final_url or "error_code" in final_url or "error_msg" in final_url:
        raise RuntimeError(
            "小红书链接无效或缺少 xsec_token 参数。\n"
            "请使用小红书 App「复制链接」功能获取分享链接（包含 xsec_token 参数），\n"
            "格式如：/discovery/item/xxx?xsec_token=..."
        )

    html = resp.text

    json_ld: dict[str, Any] = {}
    for blob in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            candidate = json.loads(blob.strip())
        except (TypeError, ValueError):
            continue
        candidates = candidate if isinstance(candidate, list) else [candidate]
        if isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
            candidates += candidate["@graph"]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = {item_type} if isinstance(item_type, str) else set(item_type or [])
            if types & {"SocialMediaPosting", "Article", "VideoObject", "ImageObject"}:
                json_ld = item
                break
        if json_ld:
            break

    def _mg(pattern):
        m = re.search(pattern, html, re.I | re.S)
        return m.group(1).strip() if m else ""

    page_title = _mg(r"<title>([^<]+)</title>")
    title = _mg(
        r'<meta[^>]*?\b(?:property|name)=["\']og:title["\'][^>]*?content=["\']([^"\']+)["\']'
    )
    caption = (
        _mg(r'<meta[^>]*?\bname=["\']description["\'][^>]*?content=["\']([^"\']+)["\']')
        or _mg(
            r'<meta[^>]*?\b(?:property|name)=["\']og:description["\'][^>]*?content=["\']([^"\']+)["\']'
        )
        or str(json_ld.get("articleBody") or json_ld.get("description") or "").strip()
    )
    if not title:
        title = str(json_ld.get("headline") or json_ld.get("name") or "").strip()

    author_name = _mg(r'<meta[^>]*?\bname=["\']author["\'][^>]*?content=["\']([^"\']+)["\']')
    if not author_name:
        author_name = _mg(r'"nickname"\s*:\s*"([^"]+)"')
    if not author_name:
        parts = page_title.split(" - ") if " - " in page_title else page_title.split(" | ")
        if len(parts) >= 2 and len(parts[-1]) < 20:
            author_name = parts[-1].strip()
    if not author_name:
        author = json_ld.get("author") or {}
        author_name = str(author.get("name") if isinstance(author, dict) else author or "").strip()

    publish_time = _mg(
        r'<meta[^>]*?\b(?:property|name)=["\'](?:article:published_time|datePublished)["\'][^>]*?content=["\']([^"\']+)["\']'
    )
    if not publish_time:
        publish_time = _mg(r'"time"\s*:\s*"([^"]+)"')
    if not publish_time:
        publish_time = str(json_ld.get("datePublished") or json_ld.get("uploadDate") or "").strip()

    note_id = ""
    m = re.search(r"/(?:explore|discovery/item)/([^/?]+)", final_url)
    if m:
        note_id = m.group(1)

    return {
        "platform": "xiaohongshu",
        "title": title or page_title,
        "caption": caption,
        "author_name": author_name,
        "publish_time": publish_time,
        "like_count": 0,
        "video_url": final_url,
        "cover_url": "",
        "post_id": note_id,
        "xsec_token": _extract_xsec_token(final_url) or _extract_xsec_token(source_url),
    }


def _extract_xhs_with_retries(url: str, telemetry: dict[str, int]) -> dict[str, Any]:
    """用独立会话重试小红书页面，避免临时空状态被当成无文案作品。"""
    attempts = 3
    last_data: Optional[dict[str, Any]] = None
    last_error: Optional[Exception] = None
    retry_wait_ms = 0
    request_ms = 0

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay = random.uniform(0.4, 0.8)
            time.sleep(delay)
            retry_wait_ms += round(delay * 1000)

        # 首次复用连接池；重试强制使用无 Cookie 的新会话，避免复用平台临时状态。
        session = _get_session() if attempt == 1 else _new_session()
        try:
            started = time.perf_counter()
            data = _run_xhs_request(
                lambda: _extract_xhs_initial_state(url, session=session), telemetry
            )
            request_ms += round((time.perf_counter() - started) * 1000)
            last_data = data
            if str(data.get("caption") or "").strip():
                telemetry["xhs_attempt_count"] = attempt
                telemetry["xhs_success_attempt"] = attempt
                telemetry["xhs_retry_wait_ms"] = retry_wait_ms
                telemetry["xhs_request_parse_ms"] = request_ms
                return data
            last_error = ValueError("小红书页面暂未返回正文")
            logger.info("小红书第 %d 次请求拿到作品但正文为空", attempt)
        except XhsAccessDeniedError:
            # 登录页/失效分享凭证不会因更换会话而恢复，立即反馈用户重新复制链接。
            raise
        except Exception as exc:
            request_ms += round((time.perf_counter() - started) * 1000)
            last_error = exc
            logger.info("小红书第 %d 次 INITIAL_STATE 解析失败: %s", attempt, exc)
        finally:
            if attempt > 1:
                session.close()

    # INITIAL_STATE 不可用时才走 meta 兜底；meta 有正文也可作为完整成功结果。
    fallback_session = _new_session()
    try:
        started = time.perf_counter()
        fallback_data = _run_xhs_request(
            lambda: _extract_xhs_lightweight(url, session=fallback_session), telemetry
        )
        telemetry["xhs_fallback_ms"] = round((time.perf_counter() - started) * 1000)
    except Exception as exc:
        last_error = exc
    finally:
        fallback_session.close()

    telemetry["xhs_attempt_count"] = attempts
    telemetry["xhs_retry_wait_ms"] = retry_wait_ms
    telemetry["xhs_request_parse_ms"] = request_ms
    if "fallback_data" in locals() and str(fallback_data.get("caption") or "").strip():
        telemetry["xhs_success_attempt"] = attempts + 1
        return fallback_data
    if last_data:
        # 仅作为最终降级结果，让调用方保留可识别作品并提示稍后重试补文案。
        return last_data
    if last_error:
        raise last_error
    raise ValueError("小红书未返回可解析的作品信息")


# ---------------------------------------------------------------- 汇总

def _fmt_ts(ts) -> str:
    """时间戳 → 'YYYY-MM-DD HH:MM:SS'，支持秒/毫秒/ISO 字符串。"""
    if not ts:
        return ""
    try:
        if isinstance(ts, str):
            if ts.isdigit():
                ts = float(ts)
            else:
                return ts
        if ts > 1e12:  # 毫秒
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _clean_caption(text: str) -> str:
    """生成可直接提交的文案；仅移除小红书话题内部标记。"""
    caption = str(text or "").strip()
    # 小红书接口有时把普通话题返回成「#标签[话题]#」。
    # 只匹配完整的标签结构，保留标签文字、顺序、换行和其他正文。
    return re.sub(r"#([^\s#\[\]]+)\[话题\]#", r"#\1#", caption)


def extract_link(raw: str) -> ExtractResult:
    """主入口：粘贴链接/混合文本 → 提取信息 + 转换链接。

    - 支持纯链接，也支持「文案+链接」混合文本（自动提取其中的 URL）
    - 短链接先解析为最终 URL
    - 抖音：移动分享页 JSON 解析
    - 小红书：__INITIAL_STATE__ → meta 轻量兜底
    """
    started = time.perf_counter()
    telemetry: dict[str, int] = {}
    # 1. 从混合文本中提取 URL（兼容整段复制粘贴）
    try:
        url = _extract_first_url(raw)
    except ValueError as e:
        return ExtractResult(
            success=False,
            error=str(e),
            hint="请粘贴抖音或小红书的分享链接（App 内复制链接）",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    # 2. 校验域名
    if not _is_safe_url(url):
        return ExtractResult(
            success=False,
            error="不支持的链接，仅支持抖音和小红书链接",
            hint="请粘贴抖音或小红书的分享链接（App 内复制链接）",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    # 2.5 残缺链接预检：作品 ID 明显短于标准长度时（聊天工具截断），直接拦截，
    # 不产生平台请求，也避免同链接反复重试（失败负缓存会兜底 10 分钟）
    integrity_error = _validate_url_integrity(url)
    if integrity_error:
        return ExtractResult(
            success=False,
            error=integrity_error,
            hint="请从 App 内复制完整分享链接（注意复制完整，避免被聊天工具截断）",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    try:
        # 提速优化：抖音提取内部已处理短链重定向，不先做 _resolve_short_link
        # 避免短链被解析成 douyin.com/video/xxx 后再被 _extract_douyin 重复请求一次
        is_xhs = "xiaohongshu" in url or "xhslink" in url

        # 缓存优化：先从 URL 预提取作品 ID 查缓存（同一视频不同链接命中秒回）
        post_id = _extract_post_id_from_url(url)
        cache_key = _cache_key(url)
        cache_keys = [cache_key]
        # 公开作品 ID 的完整提取结果可跨分享形式复用，降低平台偶发验证导致的重复失败。
        # 小红书共享缓存不保存分享参数，命中时再用本次输入链接生成规范链接。
        if post_id:
            cache_keys.append(_public_work_cache_key("xiaohongshu" if is_xhs else "douyin", post_id))
        cache_started = time.perf_counter()
        cached = None
        for candidate_key in cache_keys:
            cached = cache_get(candidate_key)
            if cached:
                break
        telemetry["cache_lookup_ms"] = round((time.perf_counter() - cache_started) * 1000)
        if cached:
            logger.info("缓存命中: %s", cache_key[:10])
            result = ExtractResult.from_dict(cached)
            normalized_caption = _clean_caption(result.caption)
            if normalized_caption != result.caption:
                # 兼容规则上线前的共享缓存：规范化后立即回写，后续请求无需重复处理。
                result.caption = normalized_caption
                cache_put(cache_key, result.to_dict())
            if not result.caption.strip() or result.partial:
                logger.info("忽略无完整文案的缓存结果: %s", cache_key[:10])
            else:
                if is_xhs and result.post_id:
                    result.canonical_url = _canonicalize_media_url("xiaohongshu", url, result.post_id)
                result.cache_hit = True
                result.telemetry = {**telemetry, "total_ms": round((time.perf_counter() - started) * 1000)}
                logger.info("性能分解 [%s] cache_hit=1 total=%dms cache=%dms", result.platform_raw or "unknown", result.telemetry["total_ms"], telemetry["cache_lookup_ms"])
                return result

        if is_xhs:
            # 小红书：先解析短链判断 token 情况（小红书短链较多）
            resolve_started = time.perf_counter()
            resolved = _resolve_short_link(url)
            telemetry["redirect_ms"] = round((time.perf_counter() - resolve_started) * 1000)
            data = _extract_xhs_with_retries(resolved, telemetry)
        else:
            external_started = time.perf_counter()
            data = _extract_douyin(url, telemetry)
            telemetry["platform_total_ms"] = round((time.perf_counter() - external_started) * 1000)

        platform = data["platform"]
        # 有些公开作品可解析出作品 ID 和规范链接，但平台页面暂时不返回正文。
        # 这种情况不应把整条链接判为失败：保留可用的转换结果，并明确标记为
        # “文案待补”，用户可以稍后用前端的单条重试补齐文案。
        missing_caption = not str(data.get("caption") or "").strip()
        post_id = data.get("post_id", "") or post_id
        canonical = data.get("canonical_url") or _canonicalize_media_url(
            platform, data.get("video_url") or (resolved if is_xhs else url), post_id
        )
        # 只有拿到稳定作品 ID 时，才允许降级为“已转换、文案待补”。
        # 若连作品 ID 都没有，通常是失效短链、登录页或平台错误页，不能误报成功。
        if missing_caption and not post_id:
            raise ValueError("平台未返回可识别的作品信息，请确认链接未失效或重新从 App 复制")

        result = ExtractResult(
            success=True,
            platform="抖音" if platform == "douyin" else "小红书",
            platform_raw=platform,
            title=data.get("title", ""),
            caption=_clean_caption(data.get("caption", "")),
            author_name=data.get("author_name", ""),
            publish_time=data.get("publish_time", ""),
            like_count=data.get("like_count", 0) or 0,
            video_url=data.get("video_url", ""),
            canonical_url=canonical,
            cover_url=data.get("cover_url", ""),
            post_id=post_id,
            hint=(
                data.get("hint", "")
                or ("链接已转换，但平台暂未返回完整文案；可稍后重试这条链接补齐文案。" if missing_caption else "")
            ),
            partial=bool(data.get("partial", False)) or missing_caption,
        )
        # 写入作品缓存（同一作品后续命中）
        if post_id:
            cache_write_started = time.perf_counter()
            cache_put(cache_key, result.to_dict())
            if not result.partial and result.caption.strip():
                public_result = result.to_dict()
                if platform == "xiaohongshu":
                    # 分享参数可能包含来源标识，不放入跨链接共享缓存。
                    public_result["canonical_url"] = _canonicalize_media_url("xiaohongshu", "", post_id)
                cache_put(_public_work_cache_key(platform, post_id), public_result)
            telemetry["cache_write_ms"] = round((time.perf_counter() - cache_write_started) * 1000)
        telemetry["total_ms"] = round((time.perf_counter() - started) * 1000)
        result.telemetry = telemetry
        logger.info("性能分解 [%s] %s", platform, " ".join(f"{k}={v}ms" for k, v in telemetry.items() if k.endswith("_ms")))
        return result
    except Exception as e:
        # 已知业务错误（作品不存在/缺参数等）对用户有用，保留友好文案；只进日志，不泄露堆栈
        if isinstance(e, (ValueError, RuntimeError)):
            logger.warning("提取失败: %s | 原因: %s", url[:80], e)
            telemetry["total_ms"] = round((time.perf_counter() - started) * 1000)
            logger.info("性能分解 [failed] %s", " ".join(f"{k}={v}ms" for k, v in telemetry.items() if k.endswith("_ms")))
            return ExtractResult(
                success=False,
                error=f"提取失败: {str(e)}",
                hint="请检查链接是否正确、作品是否公开可见、小红书链接是否带 xsec_token",
                telemetry=telemetry,
            )
        # 未知异常：脱敏，仅记录详细日志（路径/库名/堆栈不外泄）
        logger.exception("提取失败(异常): %s", url[:80])
        telemetry["total_ms"] = round((time.perf_counter() - started) * 1000)
        return ExtractResult(
            success=False,
            error="提取失败，请稍后重试",
            hint="请检查链接是否正确、作品是否公开可见",
            telemetry=telemetry,
        )
