"""2.0 前端产物（vite）的服务端（P5 拆包自 app.py）。

不用 `send_from_directory`：它返回 direct_passthrough 流式响应，会被 `_optimize_response`
的 gzip 分支跳过，导致 JS/CSS 未压缩传输。这里自己读盘 + 预压缩 + 长缓存
（文件名带内容 hash，内容变则文件名变，所以可以 immutable）。
"""

from __future__ import annotations

import gzip
import mimetypes
import os

from flask import Response, jsonify, request

from . import app
from .config import BASE_DIR



# ------------------------------------------------------- 2.0 前端产物（vite）
# frontend/ 的构建输出。文件名带内容 hash → 可以长缓存；Flask 侧补 gzip。
_ASSET_DIR = BASE_DIR / "app" / "static" / "dist" / "assets"

_ASSET_CACHE: dict = {}  # rel -> (raw, gz, ctype)

_ASSET_CACHE_MAX = 64



def _v2_index_ready() -> bool:
    """v2 产物是否就绪；没跑过 build 时回退 v1，保证 clone 下来直接能跑。"""
    return (BASE_DIR / "app" / "static" / "dist" / "index.html").is_file()



def _v2_admin_ready() -> bool:
    """P4：后台看板 2.0 产物 —— 多入口构建的另一半（dist/admin.html）。"""
    return (BASE_DIR / "app" / "static" / "dist" / "admin.html").is_file()



@app.route("/assets/<path:rel>")
def assets(rel):
    """发 vite 产物（2.0）。

    不用 send_from_directory：它返回 direct_passthrough 流式响应，
    会被 _optimize_response() 的 gzip 分支跳过，导致 JS/CSS 未压缩传输。
    这里自己读盘 + 预压缩 + 长缓存（文件名带 hash，内容变则文件名变）。
    """
    if ".." in rel or rel.startswith("/"):
        return jsonify({"success": False, "error": "not found"}), 404
    root = _ASSET_DIR.resolve()
    target = (_ASSET_DIR / rel).resolve()
    if not str(target).startswith(str(root) + os.sep) or not target.is_file():
        return jsonify({"success": False, "error": "not found"}), 404

    entry = _ASSET_CACHE.get(rel)
    if entry is None:
        raw = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        entry = (raw, gzip.compress(raw, compresslevel=5), ctype)
        if len(_ASSET_CACHE) >= _ASSET_CACHE_MAX:
            _ASSET_CACHE.clear()
        _ASSET_CACHE[rel] = entry
    raw, gz, ctype = entry

    use_gz = "gzip" in (request.headers.get("Accept-Encoding") or "")
    resp = Response(gz if use_gz else raw)
    resp.headers["Content-Type"] = ctype
    if use_gz:
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(gz))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["Vary"] = "Accept-Encoding"
    return resp
