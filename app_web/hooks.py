"""Flask 全局请求钩子（P5 拆包自 app.py）。

5 个 `@app.before_request` / `@app.after_request` 钩子。**执行顺序 = 注册顺序**，
本模块按原文件顺序注册，故顺序与拆包前完全一致：
  ① 请求开始打日志  ② 访问口令  ③ 管理员鉴权  ④ 请求结束打日志
  ⑤ 响应优化（gzip / 静态资源缓存头 / 基础安全响应头）

⚠️ 本模块顶层 `from . import app` —— 依赖 `app_web/__init__.py` 先把 `app` 建好再导入本模块。
"""

from __future__ import annotations

import gzip
import hmac
import time

from flask import jsonify, request

from . import app
from .config import ACCESS_TOKEN, ADMIN_TOKEN, log
from .security import _admin_session_valid


# ---------------------------------------------------------------- 请求日志

@app.before_request
def _log_request_start():
    if request.path.startswith("/api/"):
        request._start_time = time.time()
        log.info(">>> [%s] %s from %s", request.method, request.path, request.remote_addr)



# ---------------------------------------------------------------- 访问口令校验

@app.before_request
def _check_access_token():
    """轻量访问口令：环境变量 ACCESS_TOKEN 设置后生效（未设置则跳过，方便本地开发）。

    - 所有 /api/* 接口需携带请求头 X-Access-Token
    - hmac.compare_digest 安全比对防时序攻击
    - /api/health 豁免（部署探活需要）
    - /api/cover 豁免（前端 <img> 标签无法携带自定义请求头；该接口有域名白名单 + 重定向逐跳校验，仅返回图片数据）
    """
    if not ACCESS_TOKEN:
        return None  # 未配置口令，跳过校验（本地开发模式）
    if request.path.startswith("/api/") and request.path not in ("/api/health", "/api/cover"):
        provided = request.headers.get("X-Access-Token", "")
        if not provided or not hmac.compare_digest(provided, ACCESS_TOKEN):
            return jsonify({"success": False, "error": "访问口令错误或未提供"}), 401
    return None



@app.before_request
def _check_admin_token():
    """管理员接口鉴权：环境变量 ADMIN_TOKEN 设置后生效（未设置则管理接口不可用）。

    - 仅作用于 /api/admin/*，与普通访问口令完全隔离
    - 校验请求头 X-Admin-Token，hmac.compare_digest 防时序攻击
    - 未设置 ADMIN_TOKEN 时返回 503，提示配置（避免无鉴权裸奔）
    """
    if not request.path.startswith("/api/admin/"):
        return None
    if request.path in ("/api/admin/login", "/api/admin/logout"):
        return None
    if not ADMIN_TOKEN:
        return jsonify({"success": False, "error": "管理员口令未配置"}), 503
    provided = request.headers.get("X-Admin-Token", "")
    if provided and hmac.compare_digest(provided, ADMIN_TOKEN):
        return None
    if _admin_session_valid(request.cookies.get("admin_session", "")):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), request.cookies.get("csrf_token", "")):
            return jsonify({"success": False, "error": "CSRF 校验失败"}), 403
        return None
    return jsonify({"success": False, "error": "管理员口令错误或未提供"}), 401



@app.after_request
def _log_request_end(response):
    if request.path.startswith("/api/"):
        log.info("<<< [%s] %s -> %d (%.0fms)",
                 request.method, request.path, response.status_code,
                 (time.time() - request._start_time) * 1000 if hasattr(request, "_start_time") else 0)
    return response



# ---------------------------------------------------------------- API

@app.after_request
def _optimize_response(response):
    """性能优化：
    1. 静态资源浏览器缓存（1 小时）
    2. text/json 响应 gzip 压缩（传输体积降 ~70%）
    """
    # 静态资源缓存（manifest/图标等；sw.js 不缓存避免更新失效）
    # 注意：/assets/（2.0 产物）的缓存头在 assets() 路由里单独设，不走这里。
    if request.path.startswith("/static/") and "/sw.js" not in request.path:
        response.headers.setdefault("Cache-Control", "public, max-age=3600")

    # gzip 压缩（仅普通字符串响应，跳过文件响应/流式响应）
    # send_from_directory 文件响应有 Accept-Ranges 头且 body 是流，get_data() 不可用
    if (
        response.status_code == 200
        and not response.direct_passthrough
        and not response.is_streamed
        and "Accept-Ranges" not in response.headers
        # 已压缩过的（如 /assets/ 路由预压缩产物）不能再压一次，否则双重 gzip
        and not response.headers.get("Content-Encoding")
        and "gzip" in (request.headers.get("Accept-Encoding") or "")
        and response.content_type
        and response.content_type.startswith(("text/", "application/json", "application/javascript"))
    ):
        try:
            data = response.get_data()
        except RuntimeError:
            return response  # 流式/无法读取，跳过
        if data and len(data) > 500:
            gz = gzip.compress(data, compresslevel=5)
            if len(gz) < len(data):  # 压缩确实更小才用
                response.set_data(gz)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Vary"] = "Accept-Encoding"
                response.headers["Content-Length"] = str(len(gz))

    # 可压缩类型的响应一律声明 Vary（不管本次是否真的压缩了）：
    # 否则缓存可能存下「未压缩版本」，之后带 gzip 的请求也拿不到压缩版
    if response.content_type and response.content_type.startswith(
        ("text/", "application/json", "application/javascript")
    ):
        response.headers.setdefault("Vary", "Accept-Encoding")

    # 基础安全响应头（本服务公网可直连，属低成本基础防护）。
    # 刻意不设 CSP：页面含内联 <script>/<style> 并引用外部资源，配错会直接白屏。
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response
