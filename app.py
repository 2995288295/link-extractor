"""链接提取工具 - Flask 后端入口（shim）。

P5（v1.11.0）起真正的实现都在 `app_web/` 包里，本文件只负责「把 app 对象暴露出来」，
不承载任何业务逻辑。保留它有两个具体原因：

1. **部署零改动**：systemd 的 `ExecStart=… gunicorn … app:app` 与
   `WorkingDirectory=/opt/link-extractor` 一个字符都不用动。
2. **本地一键启动不变**：`start.py` 执行的是 `python app.py`（会走本文件末尾的
   `if __name__ == "__main__"` 起 dev server），去掉它就断链了。

`BASE_DIR` / `app.root_path` 等路径语义由 `app_web/config.py` 与 `app_web/__init__.py`
显式保住，均指向项目根（即本文件所在目录）。
"""

import os

from app_web import app
from app_web.config import log

# ---------------------------------------------------------------- 本地开发入口

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5003"))
    log.info("=" * 56)
    log.info("链接提取工具启动中...")
    log.info("访问地址: http://127.0.0.1:%d", port)
    log.info("按 Ctrl+C 停止服务")
    log.info("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
