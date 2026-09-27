"""环境变量读取、路径常量、日志装配（P5 拆包自 app.py）。

本模块是全仓**最底层**：不 import 任何兄弟模块，可被任意模块安全引用。
运维视角的环境变量入口分散在三个文件里，一眼版：
  - 本文件        BASE_DIR / DB_PATH / DEVICE_SECRET_PATH / ACCESS_TOKEN / ADMIN_TOKEN /
                  DEVICE_SECRET / EXTRACT_CONCURRENCY / PEAK_EXTRACT_CONCURRENCY /
                  GLOBAL_EXTRACT_CONCURRENCY / ADMIN_SESSION_TTL
  - ratelimit.py  RATE_IP_PER_MINUTE / RATE_DEVICE_PER_MINUTE
  - pool.py       AJIASU_POOL_BIN / AJIASU_POOL_PYTHON / AJIASU_POOL_CONFIG
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 安全配置
# ⚠️ 拆包后本文件位于 app_web/，比原来的 app.py 深一层，故多一次 .parent。
#    这是 BASE_DIR 语义保真的关键：它必须仍然等于项目根（原 app.py 所在目录），
#    否则 DB_PATH / _ASSET_DIR / dist 产物路径会全部错位。
BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = BASE_DIR / "data" / "history.db"

DEVICE_SECRET_PATH = BASE_DIR / "data" / ".device_secret"



def _load_device_secret() -> str:
    """优先使用环境变量；本机未配置时持久化随机密钥，避免历史记录因重启失效。"""
    configured = os.environ.get("DEVICE_SECRET", "").strip()
    if configured:
        return configured
    try:
        if DEVICE_SECRET_PATH.exists():
            saved = DEVICE_SECRET_PATH.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        DEVICE_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(16)
        DEVICE_SECRET_PATH.write_text(generated + "\n", encoding="utf-8")
        logging.getLogger(__name__).warning(
            "DEVICE_SECRET 未设置，已生成并保存本机密钥；生产环境请设置环境变量 DEVICE_SECRET"
        )
        return generated
    except OSError:
        generated = secrets.token_hex(16)
        logging.getLogger(__name__).warning(
            "DEVICE_SECRET 未设置且无法保存本机密钥，重启后历史设备标识将失效；生产环境请设置 DEVICE_SECRET"
        )
        return generated



ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

DEVICE_SECRET = _load_device_secret()



def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取受限整数环境变量，避免错误配置耗尽线程或触发平台风控。"""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))



# 每个批次的外部平台请求并发数。默认 3，兼顾批量速度与平台风控风险。
EXTRACT_CONCURRENCY = _bounded_int_env("EXTRACT_CONCURRENCY", 3, 1, 5)

PEAK_EXTRACT_CONCURRENCY = _bounded_int_env("PEAK_EXTRACT_CONCURRENCY", 2, 1, 5)


def _is_peak_calendar_window(now: datetime | None = None) -> bool:
    """月底和月初自动进入保守并发模式，降低同一出口 IP 的突发请求密度。"""
    current = now or datetime.now()
    next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (next_month - timedelta(days=1)).day
    return current.day <= 5 or current.day > days_in_month - 5


def _effective_extract_concurrency() -> int:
    return min(EXTRACT_CONCURRENCY, PEAK_EXTRACT_CONCURRENCY) if _is_peak_calendar_window() else EXTRACT_CONCURRENCY


GLOBAL_EXTRACT_CONCURRENCY = _bounded_int_env("GLOBAL_EXTRACT_CONCURRENCY", 2, 1, 6)

_extract_queue = ThreadPoolExecutor(max_workers=GLOBAL_EXTRACT_CONCURRENCY, thread_name_prefix="extract")


# ---------------------------------------------------------------- 日志

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging():
    """控制台日志：INFO 级别，覆盖 Flask 请求日志、提取日志、错误日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # werkzeug 访问日志也走统一格式
    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(logging.INFO)
    wlog.handlers.clear()
    wlog.addHandler(logging.StreamHandler(sys.stdout))
    wlog.propagate = False
    # 提取模块日志
    logging.getLogger("lib.extractor").setLevel(logging.INFO)
    # ⚠️ 拆包前这里写的是 getLogger(__name__)，而当时 __name__ == "app"，
    #    日志前缀即 "app:"。搬进 app_web/config.py 后 __name__ 会变成 "app_web.config"，
    #    让 journalctl / grep 的既有排查习惯失效。故显式写回 "app"，运行时语义完全等价。
    return logging.getLogger("app")



log = setup_logging()

SERVICE_STARTED_AT = time.time()



ADMIN_SESSION_TTL = 8 * 60 * 60
