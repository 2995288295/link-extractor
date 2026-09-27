"""封面图片代理（P5 拆包自 app.py）。

本接口豁免口令且公网可直连，所以三道防线一个都不能松：
  ① 域名白名单 + DNS 解析后必须为公网 IP（防 SSRF）
  ② 手动逐跳校验重定向目标（防跳转进内网）
  ③ 只认位图 Content-Type，**不透传上游**（否则会变成「同源 HTML 代理」，曾被实测复现）
"""

from __future__ import annotations

import time
from urllib.parse import urljoin as _urljoin, urlparse as _urlparse

import requests as _requests
from flask import request

from . import app
from .ratelimit import RATE_IP_PER_MINUTE, _check_rate_limit



# ---------------------------------------------------------------- 封面代理

_cover_cache: dict[str, tuple[bytes, str, float]] = {}

COVER_CACHE_SECONDS = 60 * 60 * 24  # 封面缓存 24 小时

# 只放行真正的位图格式。**不要用 image/* 通配**：image/svg+xml 是可执行脚本的
# （直接访问该 URL 时会执行），而这正是本接口要挡住的那类内容。
_ALLOWED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "image/bmp", "image/avif", "image/heic", "image/heif",
})

COVER_MAX_BYTES = 5 * 1024 * 1024           # 单张封面大小上限

COVER_CACHE_MAX_BYTES = 64 * 1024 * 1024    # 封面缓存总字节上限

_ALLOWED_IMAGE_HOSTS = (
    "douyinpic.com", "douyinimg.com", "douyinvod.com",
    "xhscdn.com", "xiaohongshu.com",
    "xhslink.com",
)



def _is_cover_url_safe(url: str) -> bool:
    """封面代理白名单：仅允许抖音/小红书的图片域名，且 DNS 解析后为公网 IP（防 SSRF）。

    注意：不复用提取器的 _is_safe_url（它只认页面域名 douyin.com/xiaohongshu.com，
    不含图片 CDN 域名 douyinpic.com 等）。这里独立做 域名白名单 + 公网 IP 校验。
    """
    try:
        import ipaddress as _ipaddress
        import socket as _socket

        parsed = _urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in _ALLOWED_IMAGE_HOSTS):
            return False
        # 公网 IP 校验（拒绝内网/回环/链路本地地址，防 SSRF）
        addresses = _socket.getaddrinfo(host, None)
        if not addresses:
            return False
        for info in addresses:
            ip = _ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False



def _fetch_cover_with_redirect_check(url: str, timeout: int = 15, max_redirects: int = 5):
    """手动跟随重定向，每跳都校验目标域名（防重定向跳转内网 SSRF）。"""
    current = url
    for _ in range(max_redirects + 1):
        if not _is_cover_url_safe(current):
            raise RuntimeError("封面地址校验失败")
        resp = _requests.get(
            current,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,  # 不一次性读进内存：由调用方按上限读取
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                return resp
            current = _urljoin(current, location)
            resp.close()  # stream=True，跳转响应体不读，显式释放连接
            continue
        return resp
    raise RuntimeError("重定向次数过多")



def _read_limited(resp, limit: int) -> bytes:
    """按上限流式读取响应体，超限即抛错。

    本接口豁免口令且公网可直连，**不能**把上游响应整个读进内存 ——
    一个几 GB 的响应就能把单 worker 打爆。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError("封面文件超过大小上限")
        chunks.append(chunk)
    return b"".join(chunks)



def _cover_cache_trim() -> None:
    """封面缓存双上限淘汰：条数超 300 或总字节超上限时，淘汰最旧的一半。"""
    total_bytes = sum(len(v[0]) for v in _cover_cache.values())
    if len(_cover_cache) <= 300 and total_bytes <= COVER_CACHE_MAX_BYTES:
        return
    stale = sorted(_cover_cache.items(), key=lambda kv: kv[1][2])[: max(1, len(_cover_cache) // 2)]
    for k, _v in stale:
        _cover_cache.pop(k, None)



@app.route("/api/cover")
def api_cover():
    """代理封面图片：绕开跨域防盗链 + 签名校验，带 24h 缓存。"""
    # 封面接口豁免口令（img 标签无法带 header），但需 IP 限速防滥用
    ip = request.remote_addr or "127.0.0.1"
    if not _check_rate_limit(f"cover:{ip}", RATE_IP_PER_MINUTE):
        return "too many requests", 429

    url = request.args.get("url", "")
    if not url or not _is_cover_url_safe(url) or len(url) > 1000:
        return "bad url", 400

    now = time.time()
    cached = _cover_cache.get(url)
    if cached and now - cached[2] < COVER_CACHE_SECONDS:
        data, ctype, _ts = cached
        resp = app.response_class(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    try:
        r = _fetch_cover_with_redirect_check(url)
        if r.status_code != 200:
            return "fetch failed", 502
        # ⚠️ 不能透传上游 Content-Type：域名白名单里有 xiaohongshu.com 整站，
        # 透传会让本接口变成「同源 HTML 代理」——本站域名可承载任意白名单域名下的
        # HTML，还能被浏览器缓存 24h（曾被实测复现）。只认位图格式，其余一律拒绝。
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in _ALLOWED_IMAGE_TYPES:
            app.logger.warning(
                "封面接口拒绝非图片响应: %s -> %s", url[:80], ctype or "(无 Content-Type)"
            )
            return "unsupported content type", 502
        try:
            declared = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            declared = 0
        if declared > COVER_MAX_BYTES:
            return "too large", 502
        data = _read_limited(r, COVER_MAX_BYTES)
        if not data:
            return "fetch failed", 502
        _cover_cache[url] = (data, ctype, now)
        _cover_cache_trim()
        resp = app.response_class(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    except Exception:
        return "fetch error", 502
