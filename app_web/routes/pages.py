"""页面与静态入口（P5 拆包自 app.py）。

`/` 与 `/admin` 各有一条 2.0 灰度通道：`?v2=1|0` 写 cookie（用户页 `ui`、后台 `adminui`，
两者**刻意分开** —— 放量节奏不同），产物缺失自动回退 v1 模板。
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
    """默认 v1（app/templates）；?v2=1 或 cookie ui=v2 走 2.0 产物。

    灰度通道：v2 产物缺失时自动回退 v1；?v2=0/1 会把选择写进 cookie，
    之后直接访问 / 就走对应版本（便于给创作者发一条带参数的链接长期试用）。
    """
    want_v2 = request.args.get("v2")
    if want_v2 == "1":
        use_v2 = True
    elif want_v2 == "0":
        use_v2 = False
    else:
        use_v2 = request.cookies.get("ui") == "v2"
    if use_v2 and not _v2_index_ready():
        log.warning("v2 产物缺失，回退 v1 首页")
        use_v2 = False

    resp = (
        send_from_directory("app/static/dist", "index.html")
        if use_v2
        else send_from_directory("app/templates", "index.html")
    )
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

    P4 起与用户页同构：?v2=1 / ?v2=0 或 cookie adminui=v2 走 2.0 产物；
    产物缺失自动回退 app/templates/admin.html（v1 底牌，原样保留）。
    灰度 cookie 与用户页的 `ui` **刻意分开** —— 两者放量节奏不同：
    用户页面向外部创作者，后台只有我们自己用。
    """
    want_v2 = request.args.get("v2")
    if want_v2 == "1":
        use_v2 = True
    elif want_v2 == "0":
        use_v2 = False
    else:
        use_v2 = request.cookies.get("adminui") == "v2"
    if use_v2 and not _v2_admin_ready():
        log.warning("v2 后台产物缺失，回退 v1 看板")
        use_v2 = False

    resp = (
        send_from_directory("app/static/dist", "admin.html")
        if use_v2
        else send_from_directory("app/templates", "admin.html")
    )
    if want_v2 in ("0", "1"):
        resp.set_cookie("adminui", "v2" if want_v2 == "1" else "v1",
                        max_age=90 * 24 * 3600, samesite="Lax", path="/")
    # 与 / 同理：HTML 写死了 hash 资源名，必须每次重验证。
    resp.headers["Cache-Control"] = "no-cache"
    return resp



@app.route("/favicon.ico")
def favicon():
    return "", 204
