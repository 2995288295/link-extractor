"""HTTP 路由层（P5 拆包自 app.py）。

子模块按「面向谁」划分：
  - `public`  用户侧 API（/api/health、/api/like、/api/extract、/api/history、/api/stats）
  - `admin`   后台运营 API（/api/admin/*）
  - `pool`    代理池运维 API（/api/admin/pool/*）—— 唯一能真改生产状态的界面
  - `pages`   页面与静态入口（/、/admin、/favicon.ico）

全部沿用 `@app.route` 注册到 `app_web/__init__.py` 的同一个 app 对象上，**未引入 Blueprint**：
全仓 `url_for` / `render_template` 命中数为 0，Blueprint 只带来改名成本而无收益。
"""

from __future__ import annotations


