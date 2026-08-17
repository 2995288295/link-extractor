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
import re
import sqlite3
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 连接池

_session_local = threading.local()


def _get_session() -> requests.Session:
    """每线程一个 Session（连接池复用），线程安全。"""
    if not hasattr(_session_local, "session"):
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session_local.session = s
    return _session_local.session


# ---------------------------------------------------------------- 结果缓存

_result_cache: dict[str, tuple[dict[str, Any], float]] = {}
CACHE_TTL_SECONDS = 24 * 60 * 60      # 24 小时
CACHE_MAX_ENTRIES = 500
_CACHE_DB = Path(__file__).resolve().parents[1] / "data" / "history.db"


def _cache_key(url: str) -> str:
    """按完整输入链接隔离缓存，避免跨用户复用带访问参数的结果。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _extract_post_id_from_url(url: str) -> str:
    """从 URL 预提取作品 ID（不发请求）：抖音 video/note/数字ID；小红书 explore/discovery/item/ID。"""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    if "douyin" in url:
        m = re.search(r"/(?:video|note)/(\d+)", path)
        return m.group(1) if m else ""
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

XHS_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.xiaohongshu.com/",
}

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
    cache_hit: bool = False

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
            "cache_hit": self.cache_hit,
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
            cache_hit=d.get("cache_hit", False),
        )


# ---------------------------------------------------------------- 抖音提取

def _parse_douyin_video_info(html: str) -> Optional[dict[str, Any]]:
    """从抖音 HTML 中提取 videoInfoRes；页面无 _ROUTER_DATA 或结构不完整时返回 None。

    注意：抖音风控时可能返回「有 _ROUTER_DATA 标记但 loaderData 是占位/不完整」的页面，
    因此不能只看正则是否匹配，必须校验能否解析出 videoInfoRes。
    """
    pattern = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", flags=re.DOTALL)
    match = pattern.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except (ValueError, TypeError):
        return None
    loader_data = data.get("loaderData") or {}
    for key in ("video_(id)/page", "note_(id)/page"):
        page = loader_data.get(key) or {}
        if page.get("videoInfoRes"):
            return page["videoInfoRes"]
    return None


def _extract_douyin(url: str) -> dict[str, Any]:
    """抖音提取：移动端分享页 _ROUTER_DATA JSON 解析。

    SDK 原版策略：Session 保持 cookie + 首次响应优先解析；解析不出
    videoInfoRes 时（无 _ROUTER_DATA 或占位页）用构造的 iesdouyin
    share URL 二次请求；仍失败则稍等带新会话重试一次，
    以对抗抖音偶发的 JS 挑战页/占位页风控响应。
    """
    source_url = _extract_first_url(url)
    session = _get_session()

    # 首次请求：手动跟随重定向到最终分享页（每跳校验目标安全）
    share_response, final_url = _safe_follow_redirects(session, source_url, headers=DOUYIN_MOBILE_HEADERS, timeout=30)
    video_id = final_url.split("?")[0].strip("/").split("/")[-1]
    share_kind = "note" if "/note/" in final_url else "video"
    share_url = f"https://www.iesdouyin.com/share/{share_kind}/{video_id}"

    video_info_res = _parse_douyin_video_info(share_response.text)
    if not video_info_res:
        # 第二次请求：构造的 iesdouyin share URL
        response, _final = _safe_follow_redirects(session, share_url, headers=DOUYIN_MOBILE_HEADERS, timeout=30)
        video_info_res = _parse_douyin_video_info(response.text)
    if not video_info_res:
        # 仍无数据：大概率是 JS 挑战页/占位页风控，等待后带新会话重试一次
        time.sleep(1.5)
        session2 = _get_session()
        for target in (share_url, source_url):
            try:
                resp, _fin = _safe_follow_redirects(session2, target, headers=DOUYIN_MOBILE_HEADERS, timeout=30)
                video_info_res = _parse_douyin_video_info(resp.text)
                if video_info_res:
                    break
            except Exception:
                continue
    if not video_info_res:
        raise ValueError(
            "从抖音 HTML 中解析视频信息失败（页面可能触发风控验证，请稍后重试）"
        )

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

def _extract_xhs_initial_state(url: str) -> dict[str, Any]:
    """小红书提取：__INITIAL_STATE__ 页面状态解析（信息最全）。"""
    source_url = _extract_first_url(url)
    response, final_url = _safe_follow_redirects(
        _get_session(), source_url, headers=XHS_HEADERS, timeout=30
    )
    response.raise_for_status()

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


def _extract_xhs_lightweight(url: str) -> dict[str, Any]:
    """小红书轻量兜底：meta 标签解析（无 __INITIAL_STATE__ 时使用）。"""
    source_url = _extract_first_url(url)
    resp = _safe_get_with_redirects(url, headers=XHS_HEADERS, timeout=10)
    resp.encoding = "utf-8"

    if "404" in resp.url or "error_code" in resp.url or "error_msg" in resp.url:
        raise RuntimeError(
            "小红书链接无效或缺少 xsec_token 参数。\n"
            "请使用小红书 App「复制链接」功能获取分享链接（包含 xsec_token 参数），\n"
            "格式如：/discovery/item/xxx?xsec_token=..."
        )

    html = resp.text

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
    )

    author_name = _mg(r'<meta[^>]*?\bname=["\']author["\'][^>]*?content=["\']([^"\']+)["\']')
    if not author_name:
        author_name = _mg(r'"nickname"\s*:\s*"([^"]+)"')
    if not author_name:
        parts = page_title.split(" - ") if " - " in page_title else page_title.split(" | ")
        if len(parts) >= 2 and len(parts[-1]) < 20:
            author_name = parts[-1].strip()

    publish_time = _mg(
        r'<meta[^>]*?\b(?:property|name)=["\'](?:article:published_time|datePublished)["\'][^>]*?content=["\']([^"\']+)["\']'
    )
    if not publish_time:
        publish_time = _mg(r'"time"\s*:\s*"([^"]+)"')

    note_id = ""
    m = re.search(r"/(?:explore|discovery/item)/([^/?]+)", resp.url)
    if m:
        note_id = m.group(1)

    return {
        "platform": "xiaohongshu",
        "title": title or page_title,
        "caption": caption,
        "author_name": author_name,
        "publish_time": publish_time,
        "like_count": 0,
        "video_url": resp.url,
        "cover_url": "",
        "post_id": note_id,
        "xsec_token": _extract_xsec_token(resp.url) or _extract_xsec_token(source_url),
    }


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
    """清洗文案：去掉小红书 [话题] 标签、> 引用前缀。"""
    if not text:
        return ""
    text = re.sub(r"\[话题\]\s*#?\s*", " ", text)
    text = re.sub(r"^>.*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_link(raw: str) -> ExtractResult:
    """主入口：粘贴链接/混合文本 → 提取信息 + 转换链接。

    - 支持纯链接，也支持「文案+链接」混合文本（自动提取其中的 URL）
    - 短链接先解析为最终 URL
    - 抖音：移动分享页 JSON 解析
    - 小红书：__INITIAL_STATE__ → meta 轻量兜底
    """
    # 1. 从混合文本中提取 URL（兼容整段复制粘贴）
    try:
        url = _extract_first_url(raw)
    except ValueError as e:
        return ExtractResult(
            success=False,
            error=str(e),
            hint="请粘贴抖音或小红书的分享链接（App 内复制链接）",
        )

    # 2. 校验域名
    if not _is_safe_url(url):
        return ExtractResult(
            success=False,
            error="不支持的链接，仅支持抖音和小红书链接",
            hint="请粘贴抖音或小红书的分享链接（App 内复制链接）",
        )

    try:
        # 提速优化：抖音提取内部已处理短链重定向，不先做 _resolve_short_link
        # 避免短链被解析成 douyin.com/video/xxx 后再被 _extract_douyin 重复请求一次
        is_xhs = "xiaohongshu" in url or "xhslink" in url

        # 缓存优化：先从 URL 预提取作品 ID 查缓存（同一视频不同链接命中秒回）
        post_id = _extract_post_id_from_url(url)
        cache_key = _cache_key(url)
        cached = cache_get(cache_key)
        if cached:
            logger.info("缓存命中: %s", cache_key[:10])
            result = ExtractResult.from_dict(cached)
            result.cache_hit = True
            return result

        if is_xhs:
            # 小红书：先解析短链判断 token 情况（小红书短链较多）
            resolved = _resolve_short_link(url)
            # 小红书缺 xsec_token 时直接报友好错误
            if "/explore/" in resolved and "xsec_token" not in resolved and "xhslink" not in resolved:
                return ExtractResult(
                    success=False,
                    error="小红书链接缺少 xsec_token 参数",
                    hint="请使用小红书 App「复制链接」获取分享链接（包含 xsec_token）",
                )
            try:
                data = _extract_xhs_initial_state(resolved)
            except Exception:
                logger.info("小红书 INITIAL_STATE 解析失败，走轻量兜底")
                data = _extract_xhs_lightweight(resolved)
        else:
            data = _extract_douyin(url)

        platform = data["platform"]
        post_id = data.get("post_id", "") or post_id
        canonical = _canonicalize_media_url(platform, data.get("video_url") or (resolved if is_xhs else url), post_id)

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
        )
        # 写入作品缓存（同一作品后续命中）
        if post_id:
            cache_put(cache_key, result.to_dict())
        return result
    except Exception as e:
        # 已知业务错误（作品不存在/缺参数等）对用户有用，保留友好文案；只进日志，不泄露堆栈
        if isinstance(e, (ValueError, RuntimeError)):
            logger.warning("提取失败: %s | 原因: %s", url[:80], e)
            return ExtractResult(
                success=False,
                error=f"提取失败: {str(e)}",
                hint="请检查链接是否正确、作品是否公开可见、小红书链接是否带 xsec_token",
            )
        # 未知异常：脱敏，仅记录详细日志（路径/库名/堆栈不外泄）
        logger.exception("提取失败(异常): %s", url[:80])
        return ExtractResult(
            success=False,
            error="提取失败，请稍后重试",
            hint="请检查链接是否正确、作品是否公开可见",
        )
