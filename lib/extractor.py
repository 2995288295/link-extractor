"""链接提取核心模块：抖音/小红书 链接 → 提取信息 + 转换链接

提炼自 social-media-toolkit SDK（开源）与 video-tools-local 项目：
- 抖音：移动端 UA 请求分享页，解析 window._ROUTER_DATA JSON（纯 requests，无签名逆向）
- 小红书：优先解析 __INITIAL_STATE__ 页面状态；失败用 meta 标签轻量兜底
- 转换链接：抖音 → douyin.com/video/{id}（去渠道参数）；小红书 → explore/{id} + 保留 xsec_token

依赖仅 requests，轻量无浏览器，适合低内存服务器部署。
"""
from __future__ import annotations

import contextlib
import ipaddress
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import socket
import subprocess
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

# ---------------------------------------------------------------- 错误归因
# 为什么要有这一层（2026-09-13 排查结论）：
# outcome_class 过去是拿报错文案去 SQL 里做关键词 LIKE 事后猜的（见 app.py 那段 CASE），
# 文案改一个字分类就崩；更糟的是根因完全不同的失败会共用同一句文案 ——
# 真风控（笔记页被 302 到 /login）、平台 404（畸形短链）、「未找到笔记详情」（作品没了）
# 全都写成「小红书平台暂时限制访问」，于是成功率统计、平台趋势、飞书告警口径一起失真。
# 现在改成：谁抛错谁给 kind，由 app.py 映射成粗粒度 outcome_class，不再猜。


class ExtractError(ValueError):
    """带归因的业务异常基类；kind 必须是 ERROR_KIND_TO_OUTCOME 的键。

    刻意继承 ValueError：既有的 `except ValueError`（含 extract_link 里的业务错误分支）
    行为完全不变，只是多带了一个 kind。
    """

    kind = "internal_error"


class InvalidInputError(ExtractError):
    """用户输入问题（不支持/残缺/缺必需参数/粘贴变形）。不计入服务失败。"""

    kind = "invalid_input"


class UnsupportedLinkError(InvalidInputError):
    kind = "unsupported_domain"


class TruncatedLinkError(InvalidInputError):
    kind = "url_truncated"


class MissingTokenError(InvalidInputError):
    kind = "missing_xsec_token"


class MalformedShortLinkError(InvalidInputError):
    """查询参数被误写进短链路径（用户手工拼接的畸形链接）。"""

    kind = "malformed_url"


class SphLinkInvalidError(InvalidInputError):
    """视频号链接短码无法识别（多为「转发文字」里不含链接）。"""

    kind = "sph_link_invalid"


class ContentExpiredError(ExtractError):
    """平台明确表示作品不存在 / 已删除 / 不可公开访问。

    ⚠️ 判死门槛（2026-10-03 v1.22.0 收紧）：只有平台**亲口说明**原因
    （抖音 filter 的中文 detail、「审核中」）才算；小红书笔记页 404 /
    页面状态容器为空一律**不**判死——平台「这次没给」≠「内容没了」，
    实测按这两个信号判死的链接 86% 重测存活（详见 CHANGELOG v1.22.0）。
    """

    kind = "expired_content"


class PlatformLimitedError(ExtractError):
    """平台风控：登录页 / 验证页。窗口式软限流，换出口 IP 可解。"""

    kind = "platform_limited"


class ShortLinkBlockedError(PlatformLimitedError):
    """短链解析阶段就被打回 /login —— 失败最集中的那一段。"""

    kind = "short_link_blocked"


class UpstreamError(ExtractError):
    """上游异常（页面结构变化、缺字段、重定向越界等），非用户问题。"""

    kind = "upstream_error"


class PageStructureError(UpstreamError):
    kind = "page_changed"


class UpstreamDataMissingError(UpstreamError):
    kind = "upstream_data_missing"


class RedirectGuardError(UpstreamError):
    kind = "redirect_blocked"


# ---------------------------------------------------------------- 用户可见文案
# 统一原则（2026-09-27，v1.7.5 收紧）：**一句话说清这条链接现在是什么状态，只给一个动作**。
# - error = 用户视角的状态。⚠️ 不要向用户汇报**我们自己的运行状况**：
#     · 不写「平台限制访问」——那是我们被平台限流，是内部运维问题，
#       对用户既无意义、又像是在说明这个工具不行；
#     · 不出现原始 URL、域名/重定向/SSRF 等技术术语（曾把
#       「链接域名或网络地址不在允许范围内: http://...」直接抛给用户）；
#     · 不出现 filter_reason / xsec_token 等字段名。
# - hint = 下一步**一个**动作，不堆互相打架的两个（曾出现
#   「请稍后重试；若持续失败请从 App 重新复制最新分享链接」——等 vs 换链接）。
# - 术语：小红书=笔记，抖音=作品；输入问题说「链接」，内容状态说「笔记/作品」。
_XHS_LIMITED_MESSAGE = "该笔记暂时无法获取"
_DOUYIN_LIMITED_MESSAGE = "该作品暂时无法获取"

# 各 error_kind → 用户提示。kind 取自本文件上面的异常类定义。
_ERROR_HINT_BY_KIND = {
    # 平台风控：窗口式软限流会自愈，系统侧已自动换 IP 重试过一次，用户只需等
    "platform_limited": "请等 1 分钟后重新提交",
    "short_link_blocked": "请等 1 分钟后重新提交",
    # 内容本身没了：重试无用，须用户自己确认
    "expired_content": "如作品仍在，请重新复制分享链接",
    # 我们这边的问题：让用户等，别让用户改输入
    "page_changed": "请稍后重试",
    "upstream_data_missing": "请稍后重试",
    "upstream_error": "请稍后重试",
    "internal_error": "请稍后重试",
    # 输入问题：给明确的纠正动作
    "url_truncated": "请重新复制完整链接",
    "missing_xsec_token": "请重新复制分享链接",
    "malformed_url": "请重新复制分享链接",
    "sph_link_invalid": "请在微信里点「分享→复制链接」后重试",
    "unsupported_domain": "请粘贴抖音、小红书或视频号的分享链接",
    "invalid_input": "请粘贴抖音、小红书或视频号的分享链接",
    "redirect_blocked": "请重新复制分享链接",
}
_DEFAULT_ERROR_HINT = "请稍后重试"


def _error_hint_for(kind: str) -> str:
    """按错误类型给用户**一个**下一步动作。"""
    return _ERROR_HINT_BY_KIND.get(str(kind or ""), _DEFAULT_ERROR_HINT)


def _douyin_filter_message(reason: str, detail: str) -> str:
    """把抖音的 filter_reason 技术代号翻成用户能读懂的一句话。

    reason 形如 `status_reviewing` / `h265_video` / `360_vr_version_control`，
    可能是 `&` 连接的多值组合；detail 是平台给的中文说明（通常为空）。
    原始 reason/detail 由调用方写日志供排查，这里只负责别把代号抛给用户
    （线上曾直接显示「抖音作品不可用（status_reviewing）：作品不存在或不可公开访问」）。
    """
    text = str(reason or "").lower()
    if "review" in text:
        return "该作品正在审核中"
    message = str(detail or "").strip()
    if message:
        return message
    return "该作品暂时无法获取"


# 细粒度 kind → 粗粒度 outcome_class。
# ⚠️ 粗粒度这一列必须只取「历史已有的 5 个值」：所有看板与告警的聚合查询都按它算，
# 而且「服务失败 = 总数 − 成功 − 用户错误」是减法算出来的 ——
# 这里凭空多出一个新取值会被减法漏掉，成功率会凭空变好。
ERROR_KIND_TO_OUTCOME = {
    "success": "success",
    # 用户输入问题
    "invalid_input": "invalid_input",
    "unsupported_domain": "invalid_input",
    "url_truncated": "invalid_input",
    "missing_xsec_token": "invalid_input",
    "malformed_url": "invalid_input",
    "sph_link_invalid": "invalid_input",
    # 内容不可用
    "expired_content": "expired_content",
    "note_missing": "expired_content",
    "not_found": "expired_content",
    # 服务失败
    "upstream_error": "upstream_error",
    # 平台限流是「换出口 IP / 换请求头可解」的一类，必须与 upstream_error 分开，
    # 否则管理看板的「平台暂时限制」恒为 0（2026-09-27 修）。
    "platform_limited": "platform_limited",
    "short_link_blocked": "platform_limited",
    "page_changed": "upstream_error",
    "upstream_data_missing": "upstream_error",
    "upstream_http": "upstream_error",
    "redirect_blocked": "upstream_error",
    "network": "upstream_error",
    "internal_error": "internal_error",
}


def error_kind_of(exc: BaseException) -> str:
    """按**异常类型**归因，绝不看报错文案。

    只有带 kind 的自定义异常、以及 requests / json 这几类标准异常能给出准确归因，
    其余一律落到 internal_error —— 宁可承认「不知道」，也不要猜成别的类别。
    """
    kind = getattr(exc, "kind", "")
    if kind:
        return kind
    if isinstance(exc, requests.exceptions.Timeout):
        return "network"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        if status == 404:
            # 平台明确说「这个资源不存在」：作品被删、短链失效，都属于内容不可用。
            return "not_found"
        if status in (401, 403, 429):
            return "platform_limited"
        if status >= 400:
            return "upstream_http"
        return "internal_error"
    if isinstance(exc, requests.exceptions.RequestException):
        return "network"
    if isinstance(exc, json.JSONDecodeError):
        return "page_changed"
    return "internal_error"


# ---------------------------------------------------------------- 连接池

_session_local = threading.local()


def _new_session() -> requests.Session:
    """创建独立会话；风控重试不能复用原会话的 Cookie。

    有生效中的代理时自动挂上代理出口（见 _ensure_proxy）。
    注意这里可能触发「按需 acquire」（约 3s）：只有此前挂过换 IP 意图时才会。
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    proxy = _ensure_proxy()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _get_session() -> requests.Session:
    """每线程一个 Session（连接池复用），线程安全。

    代理窗口开/关时切换会话，避免复用直连连接池导致请求绕过代理。
    """
    proxy = _ensure_proxy()
    session = getattr(_session_local, "session", None)
    if session is not None and getattr(_session_local, "session_proxy", "") != proxy:
        with contextlib.suppress(Exception):
            session.close()
        session = None
    if session is None:
        session = _new_session()
        _session_local.session = session
        _session_local.session_proxy = proxy
    return session


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
    if "weixin.qq.com" in url:
        m = re.search(r"weixin\.qq\.com/sph/([0-9A-Za-z]+)", url, re.I) or re.search(
            r"finder-preview/pages/sph\?[^#]*?\bid=([0-9A-Za-z]+)", url, re.I
        )
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
    # 视频号分享短链（v1.13.0）；channels.weixin.qq.com 以点后缀命中
    "weixin.qq.com",
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

# 小红书分享短链（xhslink.cn）解析专用：移动端 UA + 完整浏览器头。
# 2026-09-13 排查：短链解析若只带 User-Agent（缺 Accept/Accept-Language/Referer），
# xhslink.cn 会 302 到 www.xiaohongshu.com/login?redirectPath=...，提取链路拿到 /login
# 页面即报「小红书平台暂时限制访问」。实测同一时刻同 IP：仅 UA → 0/6，完整头 → 6/6。
# 用户从 App 复制的链接本就是移动端分享链路，此处以移动端身份请求更贴近真实。
XHS_SHARE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

# 短链解析闸门（xhslink.cn / v.douyin.com）。2026-09-27 新增。
# 此前短链阶段**完全不受任何闸门保护**：笔记页有 Semaphore(1) + 1s 间隔，
# 短链却能被多个 worker 线程同时打出去 —— 月底高峰期的瞬时并发突发源。
# 只限并发、**不加额外间隔**（min_interval=0）：真实用户点短链时浏览器会立刻
# 跟随 302 打第二跳，短链与笔记页贴在一起才像真人；被风控盯上的是「多用户
# 瞬时并发」，不是「同一用户的两次跳转」。故此处只削峰、不拖慢单条。
SHORTLINK_GATE = _PlatformGate(
    "shortlink",
    max_concurrency=int(os.environ.get("SHORTLINK_GATE_CONCURRENCY", "2")),
    min_interval=_float_env("SHORTLINK_GATE_INTERVAL", 0.0),
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
            return "链接不完整"
    elif "douyin" in host:
        m = re.search(r"/(?:video|note|share/(?:video|slides|note))/(\d+)", path, re.I)
        if m and len(m.group(1)) < 15:
            return "链接不完整"
    return ""


# 短链域（这些地址的路径里只有分享码，不该出现任何查询参数名）
SHORT_LINK_HOSTS = ("xhslink.com", "xhslink.cn", "v.douyin.com")
# 查询参数名跑进路径 = 链接被粘贴变形。
# 注意不能要求参数名前必须有 `/`：实测畸形串是 `/oxsec_token/xxx`
# （用户把 `xsec_token` 粘在了 `/o` 后面），前面的字符是字母 o 而不是斜杠。
# 短链的正常路径是 `/o/分享码` 这种短码，绝不会包含这些参数名，所以直接子串匹配。
_PARAM_LEAK_IN_PATH = re.compile(r"xsec_token|xsec_source|appuid|share_source", re.I)


def _validate_short_link_shape(url: str) -> str:
    """短链形态预检：查询参数被写进路径时直接拦截。

    2026-09-13 实测：`https://xhslink.cn/oxsec_token/4GDdLy8333r` —— 手工拼接时把
    `xsec_token` 粘进了短链路径（正常应为 `/o/4GDdLy8333r`）。这种地址打到平台只会
    拿到 404，白跑 3 次重试 + 1 次 meta 兜底共 4 个请求，还顺带推高风控计数。
    形态明显不对就在本地拦掉，一个平台请求都不发。
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 - 解析异常交给下游处理
        return ""
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in SHORT_LINK_HOSTS):
        return ""
    path = parsed.path or ""
    if _PARAM_LEAK_IN_PATH.search(path) or "=" in path:
        return "链接格式异常"
    return ""


def _safe_get_with_redirects(url: str, *, headers=None, timeout=10, max_redirects=5):
    """逐跳校验重定向目标后再请求，避免先访问内网再做检查。"""
    current = url
    for _ in range(max_redirects + 1):
        if not _is_safe_url(current):
            raise RedirectGuardError("链接地址不受支持")
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
    raise RedirectGuardError("链接无法访问")


def _safe_follow_redirects(session, url: str, *, headers=None, timeout=30, max_redirects=5):
    """手动跟随重定向（allow_redirects=False），每跳校验目标域名与公网 IP。

    用于抖音/小红书提取链路：入口 URL 已过 _is_safe_url 白名单，
    此处确保重定向目标也安全（防被劫持跳转到内网）。
    返回 (最终响应, 最终URL)；调用方用最终 URL 提取信息。
    """
    current = url
    for _ in range(max_redirects + 1):
        if not _is_safe_url(current):
            raise RedirectGuardError("链接地址不受支持")
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
    raise RedirectGuardError("链接无法访问")


# ---------------------------------------------------------------- 工具函数

def _extract_first_url(text: str) -> str:
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", text)
    if not m:
        raise InvalidInputError("未识别到有效链接")
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


def _is_xhs_bounce_page(url: str) -> bool:
    """判定 URL 是否为小红书拦截/错误页（应归为平台风控，而非用户输入问题）。

    实测两种形态（2026-09-16）：
    - 桌面头被 302 到 /login?redirectPath=...
    - 分享头被 302 到 /website-login/error?...&error_code=300011&error_msg=账号异常，请稍后重试
    后者此前未被拦截：短链解析把它当成「解析成功」，轻量兜底再用 "error_code"
    子串命中后误报成「缺 xsec_token」。注意 redirectPath 的值是 URL 编码的
    （%26xsec_token%3D...），用 query 精确匹配 error_code=/error_msg= 不会误伤。
    """
    parsed = urlparse(url)
    if parsed.path.rstrip("/") in ("/login", "/website-login/error"):
        return True
    query = parsed.query or ""
    return "error_code=" in query or "error_msg=" in query


def _request_short_link(url: str, headers: dict) -> tuple[str, str]:
    """请求一次短链并分类结果：("ok"|"blocked"|"unresolved"|"error", 解析结果)。

    - ok         拿到最终 URL
    - blocked    被平台 302 到 /login（出口 IP 被软限流）
    - unresolved 有响应但没跳转（短链页直接返回 200），无法判定，交下游
    - error      请求本身失败（网络/DNS/超时），交下游
    """
    # 走短链闸门（只限并发、不加间隔）：防止多线程同时向外打短链请求。
    # 详见 SHORTLINK_GATE 定义处的说明。
    SHORTLINK_GATE.acquire()
    try:
        try:
            resp = _safe_get_with_redirects(url, timeout=10, headers=headers)
        except Exception as e:  # noqa: BLE001 - 解析失败不阻断主链路
            logger.warning("短链接解析失败: %s", e)
            return "error", ""
        resolved = (resp.url or "").strip()
        if not resolved or resolved == url:
            return "unresolved", ""
        if _is_xhs_bounce_page(resolved):
            return "blocked", ""
        return "ok", resolved
    finally:
        SHORTLINK_GATE.release()


# ------------------------------------------------- 短链解析结果缓存（v1.12.1）

_SHORTLINK_CACHE_TTL = float(os.environ.get("SHORTLINK_CACHE_TTL_SECONDS", "1800"))
_shortlink_cache: dict = {}  # url -> [resolved_url, ts]
_shortlink_cache_lock = threading.Lock()


def _shortlink_resolved_cached(url: str) -> str:
    """短链 → 笔记页 URL 的 30min 结果缓存；命中省掉 302 那一跳（约 300ms+）。

    只缓存「解析出了不同地址」的结果：失败抛异常 / 返回原 URL 都不写缓存，
    失败重试语义由死链缓存与兜底链路各管各的。xsec_token 就在解析出的
    URL 上，30min 内有效。进程内记忆，重启即清（代价=重新解析一次）。
    """
    now = time.monotonic()
    with _shortlink_cache_lock:
        entry = _shortlink_cache.get(url)
        if entry:
            if now - entry[1] <= _SHORTLINK_CACHE_TTL:
                logger.info("短链解析缓存命中: %s", url[:60])
                return entry[0]
            del _shortlink_cache[url]
    resolved = _resolve_short_link(url)
    if resolved != url:
        with _shortlink_cache_lock:
            if len(_shortlink_cache) > 500:  # 防膨胀：删最老一条
                oldest = min(_shortlink_cache, key=lambda k: _shortlink_cache[k][1])
                del _shortlink_cache[oldest]
            _shortlink_cache[url] = [resolved, now]
    return resolved


def _resolve_short_link(url: str) -> str:
    """解析短链接：xhslink.cn / v.douyin.com → 最终 URL

    必须发送完整请求头：只带 User-Agent 会被 xhslink.cn 判为机器人并 302 到
    /login（详见 XHS_SHARE_HEADERS 注释）。小红书**先用移动端分享头**，若仍被
    打回 /login 再换桌面头重试一次；抖音按其移动端头发送。

    2026-09-27 调换顺序（原为桌面头优先）：实测同一批短链交替请求，桌面头
    6/6 被打回 /login、分享头 6/6 解析成功。桌面头既然必然失败，先打它就等于
    每次请求都白送一个「被平台拒绝」的负样本给风控，还多花约 300ms。
    现在与笔记页链路（_fetch_xhs_note_page）统一为「分享头优先」。

    被明确打回 /login 时**不再静默返回原 URL**（2026-09-13 修）：
    这里才是失败最集中的地方 —— 当天 35 次失败全部落在 xhslink 短链上，
    而兜底换 IP 只守着笔记页和抖音两条链路，短链这道门是开着的。
    现在短链阶段同样计入风控计数，达到阈值即开代理窗口并当场重试；
    重试仍失败则抛 ShortLinkBlockedError（归因 platform_limited），
    既避免再拿一个已知被限流的地址去打 3 次笔记页请求，也让告警口径正确。
    """
    if "xhslink" in url:
        candidates = (XHS_SHARE_HEADERS, XHS_HEADERS)
        platform = "xiaohongshu"
    elif "douyin" in url:
        candidates = (DOUYIN_MOBILE_HEADERS,)
        platform = "douyin"
    else:
        return url

    status, resolved = _request_short_link(url, candidates[0])
    if status == "ok":
        logger.info("短链接已解析: %s → %s", url[:50], resolved[:80])
        return resolved
    if status == "blocked":
        for headers in candidates[1:]:
            logger.warning("短链解析被打回 /login，改用备用请求头重试: %s", url[:60])
            fallback_status, fallback_resolved = _request_short_link(url, headers)
            if fallback_status == "ok":
                logger.info("短链接已解析（备用请求头）: %s → %s", url[:50], fallback_resolved[:80])
                return fallback_resolved
            if fallback_status != "blocked":
                status = fallback_status
                break

    if status != "blocked":
        # 非风控（未跳转 / 请求异常）：保持原行为，把地址交给下游平台请求去处理。
        return url

    # 明确被平台打回 /login = 出口 IP 被窗口式软限流。
    # 计入风控计数（与笔记页链路共用同一份计数），达阈值自动开代理窗口。
    proxy_ready = _note_risk_failure(platform)
    if proxy_ready and _ensure_proxy():
        for headers in candidates:
            retry_status, retry_resolved = _request_short_link(url, headers)
            if retry_status == "ok":
                logger.info("短链解析换 IP 后成功: %s → %s", url[:50], retry_resolved[:80])
                with _failover_lock:
                    _risk_events.clear()
                return retry_resolved
    # 第一轮换 IP 重试仍被拦 → rotate 挂意图、取新租约再补一枪（带每日保险丝）。
    # 依据（2026-09-27）：限流失败后 5 分钟内的重新提交 119/119 成功——多数失败是
    # 瞬时软限流，新一轮请求拿到新租约即可通过。以前这一枪靠用户手动重贴完成。
    if proxy_ready:
        extra = _shortlink_extra_shot(platform, url, candidates)
        if extra:
            return extra
    # 全部失败：只抛限流错误。短链阶段从不判死（v1.13.1 起就如此）：
    # 限流是窗口式软限制，判死会把活链错标（晚间高峰曾成片冤案）。
    # v1.22.0 起死链缓存整体退役，全链路不再有「错标 24h」的问题。
    raise ShortLinkBlockedError(_XHS_LIMITED_MESSAGE)


# ------------------------------------------- 短链补一枪（2026-09-27 v1.12.0）

_EXTRA_SHOT_MAX_PER_DAY = int(os.environ.get("AJIASU_EXTRA_SHOT_MAX_PER_DAY", "8"))
_extra_shot_state = {"date": "", "count": 0}


def _shortlink_extra_shot(platform: str, url: str, candidates) -> str:
    """rotate 挂意图、取**新**租约再试一轮短链；成功返回解析结果，失败返回空串。

    保险丝：每日最多 _EXTRA_SHOT_MAX_PER_DAY 次（默认 8）。补枪的租约按实际
    握有时长计费（AJIASU_HOLD_SECONDS=60s/次），8 次封顶约 480s，在池预算
    900s/天之内。rotate 不受 MIN_INTERVAL 限制但受 MAX_PER_HOUR 约束，
    撞上限时 _arm_failover_intent 返回 False，自动放弃。
    """
    today = time.strftime("%Y-%m-%d")
    with _failover_lock:
        if _extra_shot_state["date"] != today:
            _extra_shot_state.update({"date": today, "count": 0})
        if _extra_shot_state["count"] >= _EXTRA_SHOT_MAX_PER_DAY:
            logger.info(
                "短链补一枪跳过 [%s]：今日已达保险丝上限 %d 次",
                platform, _EXTRA_SHOT_MAX_PER_DAY,
            )
            return ""
    if not _arm_failover_intent(platform, rotate=True):
        return ""
    with _failover_lock:
        _extra_shot_state["count"] += 1
    if not _ensure_proxy():
        logger.info("短链补一枪未取到代理 [%s]，放弃", platform)
        return ""
    for headers in candidates:
        retry_status, retry_resolved = _request_short_link(url, headers)
        if retry_status == "ok":
            logger.info("短链解析补一枪成功: %s → %s", url[:50], retry_resolved[:80])
            with _failover_lock:
                _risk_events.clear()
            return retry_resolved
    return ""


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
    if platform == "sph" and work_id:
        return f"https://weixin.qq.com/sph/{work_id}"
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
    # 归因（细粒度）：由抛错处直接给出，app.py 用 ERROR_KIND_TO_OUTCOME 映射成
    # 粗粒度 outcome_class。留空表示没归因成功，此时 app.py 才回退旧的文案匹配。
    error_kind: str = ""
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
            "error_kind": self.error_kind,
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
            error_kind=d.get("error_kind", ""),
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
            "hint": "该链接是抖音用户主页，不包含单条作品的文案",
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
            "hint": "该链接是抖音用户主页，不包含单条作品的文案",
        }
    if not video_id:
        raise InvalidInputError("未识别到抖音作品")

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
        raise PlatformLimitedError(_DOUYIN_LIMITED_MESSAGE)

    item_list = video_info_res.get("item_list") or []
    if not item_list:
        filter_entry = next(
            (e for e in (video_info_res.get("filter_list") or []) if isinstance(e, dict)),
            {},
        )
        reason = filter_entry.get("filter_reason") or ""
        detail = filter_entry.get("detail_msg") or filter_entry.get("notice") or ""
        # 原始代号只进日志，不给用户看（用户看不懂 h265_video 这类内部字段值）。
        logger.info("抖音未返回作品: reason=%s detail=%s", reason or "-", detail or "-")
        message = _douyin_filter_message(reason, detail)
        # 平台只给了个技术代号（如 h265_video）、没亲口说明原因：作品多半还在，
        # 只是网页端拿不到——按「暂时拿不到」归因，不判内容失效（2026-10-03 复盘：
        # 这类失败重测 12/17 直接成功）。只有平台说明原因（审核中 / 权限或已删除）
        # 才按内容失效处理。
        if message == _DOUYIN_LIMITED_MESSAGE:
            raise UpstreamDataMissingError(message)
        raise ContentExpiredError(message)

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

class XhsAccessDeniedError(PlatformLimitedError):
    """小红书将作品页跳到登录页时使用，属于不可通过重试恢复的失败。"""

    kind = "platform_limited"


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


def _fetch_xhs_note_page(
    source_url: str,
    session: Optional[requests.Session] = None,
    *,
    timeout: int = 30,
) -> "tuple[requests.Response, str]":
    """拉取小红书笔记页：按「分享头优先、桌面头回退」两套头各试一次。

    2026-09-27 实测（同一笔记页 URL、同一时刻、同一出口 IP，交替各 3 次）：
    桌面 UA 被打回 /login 3/3，移动端分享 UA 0/3 且能拿到完整笔记页。
    短链解析早有这个回退（见 _resolve_short_link），笔记页这两处此前漏了，
    于是表现为「短链解析成功、笔记页必被打回」的假限流，把平台风控误报成
    「平台暂时限制访问」，诱使运维去换出口 IP（白白烧掉免费额度）。

    两套头都被打回才算真限流，抛 XhsAccessDeniedError 交上层换 IP。
    """
    sess = session or _get_session()
    for headers in (XHS_SHARE_HEADERS, XHS_HEADERS):
        resp, final_url = _safe_follow_redirects(
            sess, source_url, headers=headers, timeout=timeout
        )
        if not _is_xhs_bounce_page(final_url):
            return resp, final_url
    raise XhsAccessDeniedError(_XHS_LIMITED_MESSAGE)


def _xhs_note_from_state(
    state: dict[str, Any]
) -> "tuple[Optional[dict[str, Any]], bool]":
    """从 __INITIAL_STATE__ 取笔记对象，兼容新旧两种页面结构。

    返回 (note, structure_broken)：
    - note             —— 笔记对象；None 表示没取到
    - structure_broken —— True = 「容器拿到了但字段对不上」，属页面结构变化（上游改动）；
                          与「笔记被删」处置方式不同，由调用方分别报错。

    结构对照（2026-09-27 实测）：
    - 新版：state["noteData"]["data"]["noteData"]
            （作者字段是 user.nickName，小写 n 开头，非 nickname）
    - 旧版：state["note"]["noteDetailMap"][*]["note"]（现已恒为空 {}，保留兼容）
    """
    new_block = (state.get("noteData") or {}).get("data") or {}
    candidate = new_block.get("noteData") if isinstance(new_block, dict) else None
    if isinstance(candidate, dict) and candidate:
        if candidate.get("noteId") or candidate.get("desc") or candidate.get("imageList"):
            return candidate, False
        return None, True

    note_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if isinstance(note_map, dict) and note_map:
        first_entry = next(iter(note_map.values()))
        if isinstance(first_entry, dict):
            note = first_entry.get("note")
            if isinstance(note, dict):
                return note, False
        return None, True
    return None, False


def _extract_xhs_initial_state(
    url: str, *, session: Optional[requests.Session] = None
) -> dict[str, Any]:
    """小红书提取：__INITIAL_STATE__ 页面状态解析（信息最全）。"""
    source_url = _extract_first_url(url)
    response, final_url = _fetch_xhs_note_page(source_url, session, timeout=30)
    if response.status_code == 404:
        # 2026-10-03 复盘（v1.22.0）：小红书对活笔记也会间歇性返回 404/空容器
        # （风控/临时异常），按这两个信号判死 86% 是冤案，还曾被死链缓存锁 24h。
        # 一律按「暂时拿不到」处理，不再判死、不再登记死链缓存。
        raise UpstreamDataMissingError("该笔记暂时无法访问")
    response.raise_for_status()

    html = response.text
    state_match = re.search(
        r"window\.__INITIAL_STATE__=(.*?)</script>", html, flags=re.DOTALL
    )
    if not state_match:
        raise PageStructureError("暂时无法获取该笔记内容")
    state_blob = state_match.group(1)
    state_blob = re.sub(r":undefined([,}])", r":null\1", state_blob)
    # 小红书偶尔返回裸 undefined（window.__INITIAL_STATE__=undefined），json.loads 会抛
    # JSONDecodeError；它继承 ValueError，会被 extract_link 当成"业务错误"，
    # 于是这句 Python 报错原文就显示在用户的「错误信息」栏里。
    try:
        state = json.loads(state_blob)
    except ValueError as exc:
        raise PageStructureError("暂时无法获取该笔记内容") from exc

    note, structure_broken = _xhs_note_from_state(state)
    if not isinstance(note, dict):
        # 区分「笔记没了」和「页面结构变了」——两者处置方式完全不同：
        # 容器非空却取不到笔记，说明字段结构和预期不一致（上游改动）；
        # 容器为空，是平台这次没给这条笔记——可能是已删除/仅自己可见，
        # 也可能是风控/临时异常（实测活笔记同样会这样返回），不判死。
        if structure_broken:
            raise PageStructureError("暂时无法获取该笔记内容")
        raise UpstreamDataMissingError("该笔记暂时无法访问")

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
    resp, final_url = _fetch_xhs_note_page(source_url, session, timeout=10)
    resp.encoding = "utf-8"

    if "404" in urlparse(final_url).path:
        raise MissingTokenError("链接无效或已失效")

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
        # 新版页面作者字段是 user.nickName（大写 N），旧版是 nickname，两者都认。
        author_name = _mg(r'"(?:nickname|nickName)"\s*:\s*"([^"]+)"')
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
            last_error = ValueError("暂时无法获取该笔记内容")
            logger.info("小红书第 %d 次请求拿到作品但正文为空", attempt)
        except XhsAccessDeniedError:
            # 登录页大多是窗口式软限流（换出口 IP 能救）；但分享凭证失效也会跳登录页，
            # 所以只在短期成簇风控时才换 IP，单条坏链接不浪费额度。
            if _note_risk_failure("xiaohongshu"):
                recovered = _xhs_via_failover(url, telemetry)
                if recovered is not None:
                    telemetry["xhs_attempt_count"] = attempt
                    telemetry["xhs_success_attempt"] = attempt
                    with _failover_lock:
                        _risk_events.clear()
                    return recovered
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
        # meta 兜底同样会撞上登录页（此时上面那个分支没走到），成簇风控时也换一次 IP。
        if _note_risk_failure("xiaohongshu"):
            recovered = _xhs_via_failover(url, telemetry)
            if recovered is not None:
                telemetry["xhs_attempt_count"] = attempts + 1
                telemetry["xhs_success_attempt"] = attempts + 1
                with _failover_lock:
                    _risk_events.clear()
                return recovered
        raise last_error
    raise UpstreamDataMissingError("该作品暂时无法获取")


# ---------------------------------------------------------------- 爱加速换 IP 兜底
# 背景：本机是云厂商固定出口 IP，小红书/抖音会对"短时间高频请求"打窗口式软限流
# （笔记页被 302 到 /login、抖音返回验证页）。换一个国内住宅出口 IP 往往立刻恢复。
#
# 触发：同一平台在 RISK_WINDOW 秒内累计 RISK_THRESHOLD 次风控失败才动手，
#       单条链接失效不会误触发，成簇爆发才换 IP。
# 生效：向 /opt/ajiasu-pool 租一个「代理窗口」，窗口内该进程所有平台请求走代理，
#       窗口结束自动释放并记账。免费额度有限，故用 窗口 + 熔断(最小间隔/每小时上限) 双重限制。
#
# 开关：优先读池级热配置 pool-config.json 的 failoverEnabled（用 `pool.py failover on|off`
#       或 admin 面板改，立刻生效，不需要重启 5003）；没有该文件时回退到环境变量
#       AJIASU_FAILOVER_ENABLED（默认 0=关）。
# 让路：默认 08:00–08:25 是签到冻结窗口，窗口内一律不换 IP——1080 全机只有一条线路，
#       抢用会打断 sheapi 签到轮换；宁可让这几分钟的风控失败照常报错。
# 整套逻辑对原有链路零侵入：未开启 / 池子无额度 / 1080 被占用 时一律安静跳过，
# 行为与改动前一致。

AJIASU_FAILOVER_ENABLED = os.environ.get("AJIASU_FAILOVER_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
AJIASU_POOL_BIN = os.environ.get("AJIASU_POOL_BIN", "/opt/ajiasu-pool/pool.py")
AJIASU_POOL_PYTHON = os.environ.get("AJIASU_POOL_PYTHON", "/usr/bin/python3")
# 池级热配置：兜底开关（failoverEnabled）与签到冻结窗口（freezeStart/freezeEnd）都在这里。
# 该文件存在时以它为准（改文件即生效，无需重启 5003）；不存在时回退到下面的环境变量。
AJIASU_POOL_CONFIG = os.environ.get("AJIASU_POOL_CONFIG", "/opt/ajiasu-pool/pool-config.json")
AJIASU_HOLD_SECONDS = _float_env("AJIASU_FAILOVER_HOLD_SECONDS", 60, 10, 900)
AJIASU_INTENT_TTL = _float_env("AJIASU_FAILOVER_INTENT_TTL", 300, 10, 3600)
AJIASU_ACQUIRE_WAIT = _float_env("AJIASU_ACQUIRE_WAIT", 5, 0, 60)
AJIASU_MIN_INTERVAL = _float_env("AJIASU_FAILOVER_MIN_INTERVAL", 30, 0, 3600)
AJIASU_MAX_PER_HOUR = int(_float_env("AJIASU_FAILOVER_MAX_PER_HOUR", 6, 0, 200))
AJIASU_RISK_WINDOW = _float_env("AJIASU_FAILOVER_RISK_WINDOW", 90, 5, 3600)
AJIASU_RISK_THRESHOLD = int(_float_env("AJIASU_FAILOVER_RISK_THRESHOLD", 2, 1, 50))
AJIASU_POOL_TIMEOUT = _float_env("AJIASU_FAILOVER_POOL_TIMEOUT", 45, 5, 120)

# 「持续限流」升级档。判据：AJIASU_ESCALATE_RISK_WINDOW（默认 300s = 「5 分钟内没缓解」）
# 内风控次数达到 AJIASU_ESCALATE_THRESHOLD（默认 3）→ 判定换到的出口也不行，
# **强制换一条线路**（挂意图时带 rotate）。
# 注意：v1.7.0 起升级档**不再拉长窗口**。旧实现把窗口拉到 300s，但实测那 300s 里
# 一个请求都没有（额度纯亏），真正的解法是「按需取代理」——见 _ensure_proxy()。
AJIASU_ESCALATE_RISK_WINDOW = _float_env("AJIASU_FAILOVER_ESCALATE_RISK_WINDOW", 300, 30, 3600)
AJIASU_ESCALATE_THRESHOLD = int(_float_env("AJIASU_FAILOVER_ESCALATE_THRESHOLD", 3, 2, 100))

_RISK_CONTROL_MARKERS = ("平台暂时限制", "触发验证", "登录页")

_failover_lock = threading.Lock()
# gthread 单进程 4 线程共享这份状态：acquire 必须「单飞」，否则 4 个线程会同时抢
# 1080（全机单连接）并互相踩租约。Condition 让其余线程等第一个人的结果。
_failover_cond = threading.Condition(_failover_lock)
_failover_state: dict[str, Any] = {
    "proxy": "", "active_until": 0.0, "lease_id": "", "account": "", "exit_ip": "", "timer": None,
    # 换 IP 意图：达阈值只挂意图，真有请求要走代理时才 acquire（v1.7.0）
    "intent_platform": "", "intent_until": 0.0, "acquiring": False,
}
_failover_history: list[float] = []
_risk_events: list[float] = []
_risk_events_long: list[float] = []  # 长窗口（默认 300s）内的风控时刻，用于判定「持续未缓解」


def _current_proxy() -> str:
    """当前是否处于代理窗口内；是则返回代理地址，否则空串。"""
    state = _failover_state
    if state["proxy"] and time.monotonic() < float(state["active_until"] or 0.0):
        return str(state["proxy"])
    return ""


def _is_risk_control_error(exc: BaseException) -> bool:
    """异常是否属于平台风控（换出口 IP 可解）。

    ⚠️ 2026-09-27：主判据改为**异常类型**。此前只看 `_RISK_CONTROL_MARKERS`
    去匹配报错文案里的「平台暂时限制」「触发验证」—— 而那是**用户可见文案**，
    措辞改一个字就会让抖音的换 IP 兜底静默失效。文案匹配只留作旧路径兜底。
    （XhsAccessDeniedError / ShortLinkBlockedError 都是 PlatformLimitedError 子类。）
    """
    if isinstance(exc, PlatformLimitedError):
        return True
    return any(marker in str(exc) for marker in _RISK_CONTROL_MARKERS)


def _pool_config() -> dict[str, Any]:
    """读取池级热配置；读不到就返回空字典（由调用方决定默认行为）。"""
    try:
        with open(AJIASU_POOL_CONFIG, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _failover_switch_on() -> bool:
    """换 IP 兜底是否启用：热开关文件优先，读不到则回退到环境变量默认值。

    热开关由 `pool.py failover on|off` 或 admin 面板写入，改完立刻生效——
    gunicorn 不热加载，重启 5003 代价太大，开关不能做成需要重启的东西。
    """
    config = _pool_config()
    if "failoverEnabled" in config:
        return bool(config.get("failoverEnabled"))
    return AJIASU_FAILOVER_ENABLED


def _hhmm_to_minutes(value: Any, fallback: int) -> int:
    """把 "HH:MM" 换成当天分钟数；解析失败回退默认值。"""
    try:
        hour, minute = str(value).split(":", 1)
        total = int(hour) * 60 + int(minute)
        if 0 <= total < 24 * 60:
            return total
    except (AttributeError, ValueError):
        pass
    return fallback


def _in_freeze_window() -> bool:
    """是否处于签到冻结窗口（默认 08:00–08:25）。

    窗口内一律不换 IP：爱加速同一设备同时只能连一条线路，抢用会打断
    sheapi 签到轮换。窗口与代理池共用同一份配置，默认宁可多让路几分钟。
    """
    start, end, enabled = 8 * 60, 8 * 60 + 25, True
    config = _pool_config()
    if config:
        enabled = bool(config.get("freezeEnabled", True))
        start = _hhmm_to_minutes(config.get("freezeStart"), start)
        end = _hhmm_to_minutes(config.get("freezeEnd"), end)
    if not enabled:
        return False
    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    return (start <= minutes < end) if start <= end else (minutes >= start or minutes < end)


def _pool_call(args: list[str], timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
    """调用代理池 CLI，取最后一行 JSON。

    这里刻意不做开关检查：release 必须永远能执行，否则「窗口开着时把开关关掉」
    会让租约泄漏到超时。开关判断放在触发侧（_arm_failover_intent / _ensure_proxy）。
    """
    try:
        proc = subprocess.run(
            [AJIASU_POOL_PYTHON, AJIASU_POOL_BIN, *args],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout or AJIASU_POOL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("爱加速代理池调用异常 args=%s err=%s", args, exc)
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except ValueError:
                continue
    return None


def _end_failover_window() -> None:
    """代理窗口到期：释放租约，恢复直连。"""
    with _failover_lock:
        if not _failover_state["proxy"]:
            return
        timer = _failover_state.get("timer")
        _failover_state.update({
            "proxy": "", "active_until": 0.0, "lease_id": "",
            "account": "", "exit_ip": "", "timer": None,
        })
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.cancel()
    result = _pool_call(["release", "--json"], timeout=20) or {}
    logger.info("爱加速换 IP 窗口结束，已释放代理（%s）", result.get("state") or "unknown")


def _arm_failover_intent(platform: str, rotate: bool = False) -> bool:
    """挂起「换 IP 意图」——**不租代理**，等真有请求要用时才租。

    v1.7.0 的核心改动。旧实现一达阈值就立刻 acquire 一个固定时长的窗口并开始计时，
    而线上流量稀疏（1–2 分钟一条）→ 窗口大多开在没请求的空档里，额度白扣：
    2026-09-16 实测 17 个窗口、1261.7s 额度只承载了约 8 个请求，其中一笔 300s 的
    升级档窗口覆盖 0 个请求。现在拆成两段：

      · 达阈值 → 只挂意图（本函数），**零消耗**；
      · 真有请求要走代理时 → _ensure_proxy() 才 acquire（实测约 3s），拿到后握
        AJIASU_HOLD_SECONDS，到期释放。

    rotate=True（升级档）表示「当前这条线路不行」：先把已有租约放掉，逼下一次
    acquire 换一条。
    """
    if not _failover_switch_on():
        logger.info("跳过爱加速换 IP [%s]：热开关已关闭", platform)
        return False
    if _in_freeze_window():
        logger.info(
            "跳过爱加速换 IP [%s]：当前处于签到冻结窗口（默认 08:00–08:25），让路给签到轮换",
            platform,
        )
        return False
    if rotate:
        _end_failover_window()
    with _failover_lock:
        now = time.monotonic()
        _failover_history[:] = [t for t in _failover_history if t > now - 3600]
        if len(_failover_history) >= AJIASU_MAX_PER_HOUR:
            logger.info("跳过爱加速换 IP [%s]：本小时已达上限 %d 次", platform, AJIASU_MAX_PER_HOUR)
            return False
        if not rotate and _failover_history and now - _failover_history[-1] < AJIASU_MIN_INTERVAL:
            logger.info("跳过爱加速换 IP [%s]：距上次不足 %.0fs", platform, AJIASU_MIN_INTERVAL)
            return False
        _failover_history.append(now)
        _failover_state.update({
            "intent_platform": platform,
            "intent_until": now + AJIASU_INTENT_TTL,
        })
    logger.info(
        "爱加速：挂起换 IP 意图 [%s] rotate=%s TTL=%.0fs（不预租，等真有请求要走代理时才 acquire）",
        platform, rotate, AJIASU_INTENT_TTL,
    )
    return True


def _intent_active() -> bool:
    """是否有未过期的换 IP 意图。无锁快路径：每个请求都会问一次。"""
    state = _failover_state
    return bool(state["intent_platform"]) and time.monotonic() < float(state["intent_until"] or 0.0)


def _do_acquire(platform: str) -> bool:
    """真正去池子租一个代理并启动到期释放。调用方**不得**持有 _failover_lock。"""
    hold = float(AJIASU_HOLD_SECONDS)
    lease = _pool_call([
        "acquire", "--json", "--rotate", "--ttl", str(int(hold) + 15),
        "--reason", f"{platform}-risk",
    ])
    if not lease or not lease.get("ok") or not lease.get("proxy"):
        logger.info(
            "爱加速换 IP 未取到代理 [%s]：%s",
            platform,
            (lease or {}).get("message") or (lease or {}).get("state") or "pool-error",
        )
        return False
    now = time.monotonic()
    timer = threading.Timer(hold, _end_failover_window)
    timer.daemon = True
    with _failover_lock:
        _failover_state.update({
            "proxy": str(lease["proxy"]),
            "active_until": now + hold,
            "lease_id": str(lease.get("leaseId") or ""),
            "account": str(lease.get("account") or ""),
            "exit_ip": str(lease.get("exitIp") or ""),
            "timer": timer,
        })
    timer.start()
    logger.info(
        "爱加速换 IP 生效 [%s]：account=%s node=%s exitIp=%s 握=%.0fs（按需取，计时起点=有请求时）",
        platform, lease.get("account"), lease.get("node"), lease.get("exitIp"), hold,
    )
    return True


def _ensure_proxy() -> str:
    """按需取代理：**唯一**会真正 acquire 的入口，在请求路径上调用。

    没有未过期意图时立即返回空串（零阻塞、零消耗）——这就是「空转」被消掉的地方。
    有意图时才去租，且 4 个 gthread 里只有一个真正执行 acquire，其余最多等
    AJIASU_ACQUIRE_WAIT 秒复用同一份代理；等不到就退回直连，不拖死用户请求。
    """
    proxy = _current_proxy()
    if proxy:
        return proxy
    if not _intent_active():
        return ""
    deadline = time.monotonic() + AJIASU_ACQUIRE_WAIT
    while True:
        with _failover_cond:
            proxy = _current_proxy()
            if proxy:
                return proxy
            acquiring = bool(_failover_state["acquiring"])
            # 有人在取就陪着等。这里**不能**直接查「有没有意图」：意图要等 acquire
            # 结束后才清，否则等在门外的同伴会把它误判成「没意图」而退回直连。
            if not acquiring and not _intent_active():
                return ""
            if not acquiring:
                _failover_state["acquiring"] = True
                platform = str(_failover_state["intent_platform"])
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ""
            _failover_cond.wait(remaining)
    abandoned = (not _failover_switch_on() or _in_freeze_window())
    if abandoned:
        logger.info("爱加速：意图兑现前开关已关或进入冻结窗口，放弃本次取代理")
    try:
        ok = False if abandoned else _do_acquire(platform)
    finally:
        # 意图无论成败都在这里才清空（清早了会误伤同伴，清不掉会反复 acquire）。
        with _failover_cond:
            _failover_state.update({"acquiring": False, "intent_platform": "", "intent_until": 0.0})
            _failover_cond.notify_all()
    return _current_proxy() if ok else ""


def _note_risk_failure(platform: str) -> bool:
    """记录一次风控失败；达阈值只**挂意图**（不再预租代理）。返回是否该走换 IP 重试。

    两档：
      · 短档（默认 90s 内 2 次）→ 挂一个换 IP 意图；
      · 升级档（默认 300s 内 3 次）→ 判定限流持续未缓解（换到的出口也不行），
        挂意图时带 rotate，逼下一次 acquire 换一条线路。
    两档同时满足时只走升级档（同一次调用里最多触发一档）。

    ⚠️ 这里**不花钱**。真正的 acquire 由 _ensure_proxy() 在请求路径上按需完成，
    所以「挂了一堆意图但没有请求来」不会再烧额度。
    """
    now = time.monotonic()
    with _failover_lock:
        _risk_events[:] = [t for t in _risk_events if t > now - AJIASU_RISK_WINDOW]
        _risk_events.append(now)
        streak = len(_risk_events)
        if streak >= AJIASU_RISK_THRESHOLD:
            _risk_events.clear()
        _risk_events_long[:] = [t for t in _risk_events_long if t > now - AJIASU_ESCALATE_RISK_WINDOW]
        _risk_events_long.append(now)
        long_streak = len(_risk_events_long)
        if long_streak >= AJIASU_ESCALATE_THRESHOLD:
            _risk_events_long.clear()
    armed = False
    if long_streak >= AJIASU_ESCALATE_THRESHOLD:
        logger.info(
            "爱加速：%.0fs 内风控 %d 次（限流持续未缓解）→ 升级档：挂意图并要求换一条线路",
            AJIASU_ESCALATE_RISK_WINDOW, long_streak,
        )
        armed = _arm_failover_intent(platform, rotate=True)
    elif streak >= AJIASU_RISK_THRESHOLD:
        armed = _arm_failover_intent(platform)
    # 诊断用：把两档计数与意图结果都打出来，下次触发能自证「谁凑够了阈值」。
    logger.info(
        "爱加速风控计数 [%s]：短档 %d/%d(%.0fs) 长档 %d/%d(%.0fs) → %s",
        platform, streak, AJIASU_RISK_THRESHOLD, AJIASU_RISK_WINDOW,
        long_streak, AJIASU_ESCALATE_THRESHOLD, AJIASU_ESCALATE_RISK_WINDOW,
        "已挂意图" if armed else ("已有代理" if _current_proxy() else "未达阈值或被拦"),
    )
    return bool(armed or _current_proxy())


def _xhs_via_failover(url: str, telemetry: dict[str, int]) -> Optional[dict[str, Any]]:
    """换 IP 后重试小红书。

    免费节点是共享出口，偶尔会抽到已被平台标记的 IP（实测 10 个节点里 1 个脏），
    因此仍被判限流时会再换一个节点重试一次，最后再走 meta 兜底。
    """
    telemetry["xhs_failover"] = 1
    if not _ensure_proxy():
        logger.info("小红书换 IP 兜底放弃：当前取不到代理（额度/开关/冻结窗口）")
        return None
    for attempt in (1, 2):
        try:
            data = _run_xhs_request(lambda: _extract_xhs_initial_state(url), telemetry)
            if str(data.get("caption") or "").strip():
                return data
            return None
        except XhsAccessDeniedError:
            logger.info(
                "小红书换 IP 第 %d 次仍被判限流（出口 %s）",
                attempt, _failover_state.get("exit_ip") or "?",
            )
            if attempt == 2 or not _arm_failover_intent("xiaohongshu", rotate=True):
                return None
        except Exception as exc:  # noqa: BLE001 - 兜底链路，失败即回退原错误
            logger.info("小红书换 IP 后 INITIAL_STATE 失败: %s", exc)
            return None
    try:
        data = _run_xhs_request(lambda: _extract_xhs_lightweight(url), telemetry)
        if str(data.get("caption") or "").strip():
            return data
    except Exception as exc:  # noqa: BLE001
        logger.info("小红书换 IP 后 meta 兜底仍失败: %s", exc)
    return None


# ---------------------------------------------------------------- 汇总

# ---------------------------------------------------------------- 视频号提取（v1.13.0）
# 实测（2026-10-01，9 条真实分享链接）：
# - 短链 weixin.qq.com/sph/{id} 301 → channels.weixin.qq.com/finder-preview/pages/sph?id={id}
# - 数据接口 POST /finder-preview/api/feed/get_feed_info 无 cookie/签名/登录态，
#   但 TLS/HTTP2 指纹校验会拒绝非浏览器客户端（requests/curl → permission
#   verification failed）；curl_cffi impersonate="chrome" 后 9/9 通过。
# - 成功响应是 HTTP 201（不是 200）。无独立 title 字段（取文案首行）。
# 依赖：curl_cffi（requirements.txt 已登记）。接口为公开分享预览页所用，
# 量级按平台闸门限流（SPH_GATE_* 可调）。
_SPH_API_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
_SPH_PAGE_URL = "https://channels.weixin.qq.com/finder-preview/pages/sph"

PLATFORM_LABELS = {"douyin": "抖音", "xiaohongshu": "小红书", "sph": "视频号"}

_SPH_GATE = _PlatformGate(
    "sph",
    max_concurrency=int(os.environ.get("SPH_GATE_CONCURRENCY", "2")),
    min_interval=_float_env("SPH_GATE_INTERVAL", 0.2),
)


def _sph_short_id(url: str) -> str:
    """提取视频号短码：weixin.qq.com/sph/{id} 或 finder-preview 页 ?id={id}。"""
    m = re.search(r"weixin\.qq\.com/sph/([0-9A-Za-z]+)", url, re.I)
    if not m:
        m = re.search(r"finder-preview/pages/sph\?[^#]*?\bid=([0-9A-Za-z]+)", url, re.I)
    return m.group(1) if m else ""


def _parse_sph_count(value) -> int:
    """视频号计数是格式化字符串：'32' / '1.2万' → int；失败返回 0。"""
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    m = re.fullmatch(r"([\d.]+)\s*万", text)
    if m:
        try:
            return int(float(m.group(1)) * 10000)
        except ValueError:
            return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _sph_title_from_caption(caption: str) -> str:
    """视频号无独立标题：取第一个话题标签前的正文首行（保留标点）。"""
    for line in str(caption or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head = re.split(r"#", line, maxsplit=1)[0].strip(" ，,、")
        return head or line[:30]
    return ""


def _extract_sph(url: str, telemetry: dict[str, int]) -> dict[str, Any]:
    """视频号分享链接提取：短码 → finder-preview 公开接口 → 字段映射。

    归因遵守既有体系：短码识别不了 = 用户输入（sph_link_invalid）；
    接口异常/结构变化 = upstream 系；不透传原始报错（§I6 纪律）。
    """
    short_id = _sph_short_id(url)
    if not short_id or len(short_id) < 6:
        raise SphLinkInvalidError("无法识别该视频号链接")

    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover - 部署环境已随 venv 安装
        raise UpstreamError("该视频号作品暂时无法获取") from exc

    _SPH_GATE.acquire()
    api_started = time.perf_counter()
    try:
        try:
            resp = curl_requests.post(
                f"{_SPH_API_URL}?_rid={os.urandom(8).hex()}&_pageUrl={_SPH_PAGE_URL}",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://channels.weixin.qq.com",
                    "Referer": f"{_SPH_PAGE_URL}?id={short_id}",
                },
                json={"baseReq": {"generalToken": ""}, "shortUri": short_id},
                impersonate="chrome",
                timeout=10,
            )
        except Exception as exc:
            logger.warning("视频号接口请求失败: %s", exc)
            raise UpstreamError("该视频号作品暂时无法获取") from exc
    finally:
        _SPH_GATE.release()
    telemetry["sph_api_ms"] = round((time.perf_counter() - api_started) * 1000)

    if resp.status_code not in (200, 201):
        logger.warning("视频号接口 HTTP %s: %s", resp.status_code, str(resp.text)[:200])
        raise UpstreamError("该视频号作品暂时无法获取")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PageStructureError("该视频号作品暂时无法获取") from exc
    if payload.get("errCode") != 0 or not isinstance(payload.get("data"), dict):
        logger.warning("视频号接口返回异常: %s", str(payload)[:200])
        raise UpstreamError("该视频号作品暂时无法获取")

    data = payload["data"]
    feed = data.get("feedInfo") or {}
    author = data.get("authorInfo") or {}
    caption = str(feed.get("description") or "").strip()
    return {
        "platform": "sph",
        "title": _sph_title_from_caption(caption),
        "caption": caption,
        "author_name": str(author.get("nickname") or "").strip(),
        "publish_time": _fmt_ts(feed.get("createtime")),
        "like_count": _parse_sph_count(feed.get("likeCountFmt")),
        "video_url": "",
        "canonical_url": f"https://weixin.qq.com/sph/{short_id}",
        "cover_url": str(feed.get("coverUrl") or ""),
        "post_id": short_id,
        "partial": not caption,
    }


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
            hint="请粘贴抖音、小红书或视频号的分享链接",
            error_kind=getattr(e, "kind", "invalid_input"),
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    # 2. 校验域名
    if not _is_safe_url(url):
        return ExtractResult(
            success=False,
            error="仅支持抖音、小红书或视频号链接",
            hint="请粘贴抖音、小红书或视频号的分享链接",
            error_kind="unsupported_domain",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    # 2.5 残缺链接预检：作品 ID 明显短于标准长度时（聊天工具截断），直接拦截，
    # 不产生平台请求。
    # ⚠️ 原注释写「失败负缓存会兜底 10 分钟」——**该机制并不存在**（cache_put 只在
    # 成功路径被调用），重试会实打实再打一遍平台。改注释为真，别误导排查（2026-09-27）。
    integrity_error = _validate_url_integrity(url)
    if integrity_error:
        return ExtractResult(
            success=False,
            error=integrity_error,
            hint="请重新复制完整链接",
            error_kind="url_truncated",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    # 2.6 短链形态预检：查询参数被粘进路径（如 /oxsec_token/xxx）时本地拦截。
    # 这种地址打到平台只会 404，白跑 4 个请求还会推高风控计数。
    malformed_short_link = _validate_short_link_shape(url)
    if malformed_short_link:
        return ExtractResult(
            success=False,
            error=malformed_short_link,
            hint="请重新复制分享链接",
            error_kind="malformed_url",
            telemetry={"input_ms": round((time.perf_counter() - started) * 1000)},
        )

    try:
        # 提速优化：抖音提取内部已处理短链重定向，不先做 _resolve_short_link
        # 避免短链被解析成 douyin.com/video/xxx 后再被 _extract_douyin 重复请求一次
        is_xhs = "xiaohongshu" in url or "xhslink" in url
        is_sph = "weixin.qq.com" in url.lower()
        # 缓存优化：先从 URL 预提取作品 ID 查缓存（同一视频不同链接命中秒回）
        post_id = _extract_post_id_from_url(url)
        cache_key = _cache_key(url)
        cache_keys = [cache_key]
        # 公开作品 ID 的完整提取结果可跨分享形式复用，降低平台偶发验证导致的重复失败。
        # 小红书共享缓存不保存分享参数，命中时再用本次输入链接生成规范链接。
        if post_id:
            cache_keys.append(_public_work_cache_key("xiaohongshu" if is_xhs else ("sph" if is_sph else "douyin"), post_id))
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
            resolved = _shortlink_resolved_cached(url)
            telemetry["redirect_ms"] = round((time.perf_counter() - resolve_started) * 1000)
            data = _extract_xhs_with_retries(resolved, telemetry)
        elif is_sph:
            # 视频号：短码直调 finder-preview 公开接口（无短链解析一跳）
            external_started = time.perf_counter()
            data = _extract_sph(url, telemetry)
            telemetry["platform_total_ms"] = round((time.perf_counter() - external_started) * 1000)
        else:
            external_started = time.perf_counter()
            try:
                data = _extract_douyin(url, telemetry)
            except ValueError as exc:
                # 抖音「触发验证」同样是出口 IP 被软限流：成簇出现时换 IP 再试一次。
                if not _is_risk_control_error(exc) or not _note_risk_failure("douyin"):
                    raise
                try:
                    data = _extract_douyin(url, telemetry)
                    with _failover_lock:
                        _risk_events.clear()
                except Exception as retry_exc:  # noqa: BLE001 - 保留原始业务错误
                    logger.info("抖音换 IP 重试仍失败: %s", retry_exc)
                    raise exc
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
            raise UpstreamDataMissingError("该作品暂时无法获取")

        result = ExtractResult(
            success=True,
            error_kind="success",
            platform=PLATFORM_LABELS.get(platform, platform),
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
                or ("文案暂未获取完整，可重试补齐" if missing_caption else "")
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
        # 归因按**异常类型**走（error_kind_of），不再对报错文案做关键词匹配。
        kind = error_kind_of(e)
        # 只信我们自己定义的业务异常（ExtractError 及其子类）能带用户文案。
        # 此前这里是 (ValueError, RuntimeError) —— 任何库抛的 ValueError 都会被原样透传
        # （典型：json.JSONDecodeError → 用户看到 "Expecting value: line 1 column 1 (char 0)"）。
        # 未知异常一律走下面的脱敏分支，详细堆栈只进日志。
        if isinstance(e, ExtractError):
            logger.warning("提取失败 [%s]: %s | 原因: %s", kind, url[:80], e)
            telemetry["total_ms"] = round((time.perf_counter() - started) * 1000)
            logger.info("性能分解 [failed] %s", " ".join(f"{k}={v}ms" for k, v in telemetry.items() if k.endswith("_ms")))
            return ExtractResult(
                success=False,
                error=str(e),
                hint=_error_hint_for(kind),
                error_kind=kind,
                telemetry=telemetry,
            )
        # 未知异常：脱敏，仅记录详细日志（路径/库名/堆栈不外泄）
        logger.exception("提取失败(异常) [%s]: %s", kind, url[:80])
        telemetry["total_ms"] = round((time.perf_counter() - started) * 1000)
        return ExtractResult(
            success=False,
            error="提取失败",
            hint=_error_hint_for(kind),
            error_kind=kind,
            telemetry=telemetry,
        )
