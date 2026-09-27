"""链接提取工具 - Flask 后端（应用装配层）。

P5（v1.11.0）把原先 1969 行的单文件 `app.py` 拆成 `app_web/` 包。本模块只管三件事：
  ① 建 Flask 应用对象并配置；
  ② 导入钩子 / 路由模块（它们反向 `from . import app`）；
  ③ 按序跑一次性初始化（`_init_db()` → `_load_rate_buckets()`）。

⚠️ 为什么保留**模块级 app** 而不引入 `create_app()` 工厂（有意偏离方案 §10.1 的写法）：
   本文件有 32 处 `@app.route`。若把路由定义搬进工厂作用域，endpoint 名会变成
   `create_app.<locals>.api_xxx`；而全仓 `url_for` / `render_template` / `current_app`
   命中数均为 0 —— 工厂带来的可测性收益在这里是零，纯粹扩大改动面。
   进程级单例（`_rate_buckets` / `_extract_queue` / `_db_local` …）由各模块的模块级变量天然保证。

⚠️ 部署入口**零改动**：`gunicorn app:app` 里的 `app` 仍是根目录的 shim `app.py`，
   它只做 `from app_web import app`。systemd unit、WorkingDirectory、`python app.py` 都不变。
"""

from __future__ import annotations

from flask import Flask

from .config import BASE_DIR
from .db import _init_db
from .ratelimit import _load_rate_buckets

# ---------------------------------------------------------------- 应用对象

# ⚠️ 必须显式传 root_path：Flask 用它解析相对 static_folder 与
#    send_from_directory 的相对目录。不传时 Flask 会按 import_name 推断，
#    而本模块的 __name__ 是 "app_web" → root_path 会变成 app_web/，
#    导致 /static/* 与 send_from_directory("app/templates", …) 全部 404。
app = Flask(
    "app_web",
    root_path=str(BASE_DIR),
    static_folder="app/static",
    static_url_path="/static",
)

app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # 请求体 512KB

# ---------------------------------------------------------------- 装配
# ⚠️ 钩子与路由模块内部写的是 `from . import app` / `from .. import app`，
#    拿的就是本模块此刻已经建好的这个 app 对象 —— 所以**必须在建好 app 之后**才导入它们。
#    （导入顺序 = 路由注册顺序，与原文件顺序保持一致。）
#
# ⚠️⚠️ 这份清单 = **所有会注册 @app.route / @app.before_request 的模块**，
#      漏一个就是静默丢路由（P5 首轮实测就漏了 cover.py → /api/cover 整个 404）。
#      生成器末尾的 [7] 项会机械校验「定义了 @app.* 的模块必须出现在本清单里」。
from . import assets, cover, hooks  # noqa: E402,F401
from .routes import admin, pages, public  # noqa: E402,F401
from .routes import pool as pool_routes  # noqa: E402,F401   # 别名：别和 app_web.pool 撞名

# ---------------------------------------------------------------- 一次性初始化
# 原文件这两句是顶层副作用调用（原 L378 / L525），顺序即语义：先建表，再载入限速窗口。

_init_db()
_load_rate_buckets()
