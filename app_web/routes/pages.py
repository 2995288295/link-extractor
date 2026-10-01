"""页面与静态入口（P5 拆包自 app.py）。

2.0（vite 产物）自 **v1.15.0（2026-10-01）起为默认**：`/` 与 `/admin` 默认即 2.0，
仅 `?v2=0` 显式回退 v1 化石模板（已无维护成本，留作逃生门），v1 产物缺失时也自动回落。
旧灰度 cookie（`ui=v1` / `adminui=v1`）不再生效—— 早期体验成员的浏览器里可能还留着它，
默认 v2 可扫掉这批旧状态，避免「明明更新了却还看旧页」。
⚠️ `send_from_directory` 的目录参数是相对路径，依赖 `app.root_path` == 项目根 ——
   见 `app_web/__init__.py` 里 `Flask(...)` 显式传 `root_path` 的那段注释。
"""

from __future__ import annotations

from flask import request, send_from_directory

from .. import app
from ..assets import _v2_admin_ready, _v2_index_ready
from ..config import log



# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    """默认 2.0（app/static/dist）；`?v2=0` 显式回退 v1 化石模板。

    灰度文档（历史）：v1.8.0–v1.14.0 曾默认 v1、`?v2=1` 或 cookie ui=v2 走 2.0，
    放量完成后于 v1.15.0 把默认反转；`?v2=0/1` 仍写 cookie（90 天），
    方便发一条带参数的链接给成员临时试用某个版本。
    """
    use_v2 = request.args.get("v2") != "0"
    if use_v2 and not _v2_index_ready():
        log.warning("v2 产物缺失，回退 v1 首页")
        use_v2 = False

    resp = (
        send_from_directory("app/static/dist", "index.html")
        if use_v2
        else send_from_directory("app/templates", "index.html")
    )
    want_v2 = request.args.get("v2")
    if want_v2 in ("0", "1"):
        resp.set_cookie("ui", "v2" if want_v2 == "1" else "v1",
                        max_age=90 * 24 * 3600, samesite="Lax", path="/")
    # HTML 必须每次重验证：它写死了 hash 资源名，缓存旧 HTML 会指向已删文件 → 白屏。
    # （2026-09-27 实测：Flask 对未配置 max_age 的文件响应默认就是 no-cache，
    #   现状本来安全；显式写出来是为了表达意图 + 防 Flask 版本行为变化。）
    resp.headers["Cache-Control"] = "no-cache"
    return resp



@app.route("/admin")
def admin_page():
    """后台运营看板页面（鉴权由前端 + /api/admin/* 双重保障）。

    与用户页同构：v1.15.0 起默认 2.0 产物，`?v2=0` 显式回退 v1 底牌。
    """
    use_v2 = request.args.get("v2") != "0"
    if use_v2 and not _v2_admin_ready():
        log.warning("v2 后台产物缺失，回退 v1 看板")
        use_v2 = False

    resp = (
        send_from_directory("app/static/dist", "admin.html")
        if use_v2
        else send_from_directory("app/templates", "admin.html")
    )
    want_v2 = request.args.get("v2")
    if want_v2 in ("0", "1"):
        resp.set_cookie("adminui", "v2" if want_v2 == "1" else "v1",
                        max_age=90 * 24 * 3600, samesite="Lax", path="/")
    # 与 / 同理：HTML 写死了 hash 资源名，必须每次重验证。
    resp.headers["Cache-Control"] = "no-cache"
    return resp



@app.route("/favicon.ico")
def favicon():
    return "", 204
