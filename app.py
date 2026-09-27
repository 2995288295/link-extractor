"""链接提取工具 - Flask 后端

功能：
- POST /api/extract   批量提取（多行链接 → 每条独立结果）
- GET/POST /api/history  历史记录（按设备 ID 隔离）
- GET /api/health    健康检查
- GET /              前端页面

部署：venv + gunicorn，端口 5002
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import csv
import io
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

import requests as _requests
from flask import Flask, Response, jsonify, make_response, request, send_from_directory

from lib.extractor import ERROR_KIND_TO_OUTCOME, extract_link

# ---------------------------------------------------------------- 安全配置

BASE_DIR = Path(__file__).resolve().parent
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
    return logging.getLogger(__name__)


log = setup_logging()

app = Flask(__name__, static_folder="app/static", static_url_path="/static")
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # 请求体 512KB
SERVICE_STARTED_AT = time.time()

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

# ---------------------------------------------------------------- 数据库

class _DbHandle:
    """线程独占的 SQLite 长连接句柄（薄代理）。

    为什么要代理：本文件几乎所有使用点都写成 `finally: conn.close()`。
    如果 _get_db() 直接返回复用的 Connection，第一次 close 就会把长连接关掉，
    同线程后续拿到的是已关闭连接。这里把 close() 的语义改成「回滚未提交事务」，
    连接本身留在 thread-local 里继续复用；其余属性一律转发底层连接。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def close(self):
        """不真正关闭：只丢弃未提交事务，连接留给本线程复用。"""
        try:
            self._conn.rollback()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


_db_local = threading.local()


def _new_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL：并发读写不互相阻塞；synchronous=NORMAL 是 WAL 下的官方推荐搭配
    # （只在 checkpoint 时 fsync，写入快很多；掉电最坏丢最近几笔事务，不会损坏库）
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_db():
    """取当前线程的 SQLite 连接（首次调用建立，之后复用）。

    ⚠️ 复用只在本进程内成立：gunicorn `-w 1` 单进程 4 线程 → 每线程一条连接。
    sqlite3 连接不可跨线程/跨 fork 使用，若将来改成 `-w >1` 或加 `--preload`，
    这里必须改回「每请求新建 + 真关闭」。
    """
    handle = getattr(_db_local, "handle", None)
    if handle is None:
        handle = _DbHandle(_new_db_conn())
        _db_local.handle = handle
    return handle


def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            original_url TEXT NOT NULL,
            canonical_url TEXT DEFAULT '',
            platform TEXT DEFAULT '',
            title TEXT DEFAULT '',
            caption TEXT DEFAULT '',
            author_name TEXT DEFAULT '',
            publish_time TEXT DEFAULT '',
            like_count INTEGER DEFAULT 0,
            video_url TEXT DEFAULT '',
            cover_url TEXT DEFAULT '',
            status TEXT DEFAULT 'success',
            error TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            cache_hit INTEGER DEFAULT 0,
            outcome_class TEXT DEFAULT 'success',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket_key TEXT PRIMARY KEY,
            timestamps TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_device ON history(device_id, created_at)")
    # 管理看板全局聚合查询加速
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_status ON history(status)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
    if "duration_ms" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN duration_ms INTEGER DEFAULT 0")
    if "cache_hit" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN cache_hit INTEGER DEFAULT 0")
    if "outcome_class" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN outcome_class TEXT DEFAULT 'success'")
    if "error_kind" not in columns:
        # 细粒度归因：由 lib/extractor 的异常类直接给出，不再靠文案猜。
        # 历史行留空 —— 那批数据没有这个信息，无法事后补齐，
        # 聚合查询用「error_kind = '' 时回退文案匹配」来兼容。
        conn.execute("ALTER TABLE history ADD COLUMN error_kind TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_error_kind ON history(error_kind)")
    if "retry_count" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "success_attempt" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN success_attempt INTEGER DEFAULT 0")
    if "retry_reason" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN retry_reason TEXT DEFAULT ''")
    conn.execute("UPDATE history SET outcome_class = CASE "
                 "WHEN status = 'success' THEN 'success' "
                 "WHEN error LIKE '%不支持%' OR error LIKE '%xsec_token%' OR error LIKE '%作品 ID%' THEN 'invalid_input' "
                 "WHEN error LIKE '%失效%' OR error LIKE '%不存在%' THEN 'expired_content' "
                 "WHEN error LIKE '%风控%' OR error LIKE '%HTML%' OR error LIKE '%JSON%' OR error LIKE '%请求%' THEN 'upstream_error' "
                 "ELSE 'internal_error' END WHERE status != 'success' AND (outcome_class IS NULL OR outcome_class = '' OR outcome_class = 'success')")
    conn.execute("CREATE TABLE IF NOT EXISTS extract_cache (cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_extract_cache_expiry ON extract_cache(expires_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS admin_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL, value REAL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_alerts_created ON admin_alerts(created_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, ip TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at)")
    conn.commit()
    conn.close()


def _admin_audit(action: str, ip: str):
    conn = _get_db()
    try:
        conn.execute("INSERT INTO admin_audit(action, ip, created_at) VALUES (?,?,?)", (action, ip, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def _classify_outcome(success: bool, error: str, error_kind: str = "") -> str:
    """把一次提取归到粗粒度 outcome_class。

    首选 extractor 给的 **error_kind**（谁抛错谁归因，见 lib/extractor.py 的错误归因一节）；
    只有拿不到 kind 时才回退到文案匹配。之所以要这样：过去全靠对报错文案做关键词 LIKE，
    文案改一个字分类就崩，而且根因不同的失败会共用同一句文案（真风控 / 平台 404 /
    笔记已删除 全都写成「小红书平台暂时限制访问」），成功率与告警口径一起失真。
    """
    if success:
        return "success"
    coarse = ERROR_KIND_TO_OUTCOME.get((error_kind or "").strip())
    if coarse:
        return coarse
    return _classify_outcome_by_text(success, error)


def _classify_outcome_by_text(success: bool, error: str) -> str:
    """兜底路径：对报错文案做关键词匹配。

    ⚠️ 只在没有 error_kind（历史记录、旧缓存、外部调用）时使用。
    新增抛错点时请改用 lib.extractor 里带 kind 的异常类，不要再往这里加关键词。
    """
    if success:
        return "success"
    text = (error or "").lower()
    # 只把平台已明确确认不可用的作品归为“内容失效”。
    # 不要因为提示语里带“请确认链接未失效”就误分为用户错误。
    if any(token in text for token in ("作品已删除", "作品不存在", "status_reviewing", "not_publicly_available", "平台返回 http 404")):
        return "expired_content"
    if any(token in text for token in ("不支持", "xsec_token", "作品 id", "作品id", "无法识别", "直播", "商品")):
        return "invalid_input"
    if any(token in text for token in ("风控", "验证", "captcha", "challenge", "html", "json", "请求", "超时", "网络", "完整文案", "未返回可识别")):
        return "upstream_error"
    return "internal_error"


_init_db()

# ---------------------------------------------------------------- 设备 ID（带签名防伪造）

def _sign_device_id(device_id: str) -> str:
    """用服务端密钥对 device_id 生成 HMAC-SHA256 签名。"""
    return hmac.new(DEVICE_SECRET.encode("utf-8"), device_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_device_signature(device_id: str, signature: str) -> bool:
    """校验 device_id 签名，防止伪造他人 device_id 越权访问历史/统计。"""
    if not device_id or not signature:
        return False
    if len(device_id) > 64 or len(signature) > 128:
        return False
    expected = _sign_device_id(device_id)
    return hmac.compare_digest(expected, signature)


def _get_device(req, issue_new: bool = True):
    """解析并校验请求中的 device_id + 签名。

    返回 (device_id, signature, valid)：
    - 签名有效：直接使用，valid=True
    - 无签名/签名无效：签发新设备（原请求视为新设备），valid=False
      （valid=False 表示设备身份不可信，调用方可跳过设备级限速）
    """
    device_id = req.headers.get("X-Device-Id", "")
    signature = req.headers.get("X-Device-Sig", "")
    if device_id and _verify_device_signature(device_id, signature):
        return device_id, signature, True
    if issue_new:
        new_id = str(uuid.uuid4())
        new_sig = _sign_device_id(new_id)
        return new_id, new_sig, False  # 新签发的可正常使用，但原设备身份不可信
    return device_id, signature, False


def _device_cookie_payload(device_id: str, signature: str):
    """响应中附带的设备标识载荷。"""
    return {"device_id": device_id, "device_sig": signature}


# ---------------------------------------------------------------- 速率限制（内存滑动窗口 + 定期落库）

RATE_IP_PER_MINUTE = int(os.environ.get("RATE_IP_PER_MINUTE", "20"))      # 每 IP 每分钟
RATE_DEVICE_PER_MINUTE = int(os.environ.get("RATE_DEVICE_PER_MINUTE", "15"))  # 每设备每分钟
_RATE_WINDOW = 60  # 秒

# 2026-09-27：原来是「查 SQLite → 改 list → 写回」，三步之间没有事务也没有锁，
# 单进程 4 线程会各自读到同一份旧数据再互相覆盖 ——
# 实测并发 40 次 / 限额 20 → 放行 40 次（超额 2 倍）；且每次检查都要抢一次写锁
# （40 次共 545ms，空载单次仅 0.32ms）。
# 现在改为进程内内存滑动窗口（gunicorn `-w 1` 单进程 4 线程天然共享，加锁即正确），
# 再定期把窗口快照落回 rate_limits 表（保留原表结构，重启后计数不重置）。
_rate_lock = threading.Lock()
_rate_buckets = {}          # bucket_key -> 窗口内时间戳列表（升序）
_rate_persist_lock = threading.Lock()
_rate_last_persist = 0.0
_RATE_PERSIST_INTERVAL = 15   # 秒：最多每 15 秒落库一次
_RATE_GC_THRESHOLD = 2000     # 内存桶数超过此值时顺带清理过期桶


def _rate_gc_locked(now: float):
    """清掉窗口外的桶（调用方须持有 _rate_lock）。"""
    cutoff = now - _RATE_WINDOW
    for key in [k for k, v in _rate_buckets.items() if not v or v[-1] <= cutoff]:
        del _rate_buckets[key]


def _check_rate_limit(bucket_key: str, limit: int) -> bool:
    """进程内滑动窗口限速；超限返回 False。"""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    try:
        with _rate_lock:
            timestamps = [t for t in _rate_buckets.get(bucket_key, ()) if t > cutoff]
            if len(timestamps) >= limit:
                _rate_buckets[bucket_key] = timestamps
                return False
            timestamps.append(now)
            _rate_buckets[bucket_key] = timestamps
            if len(_rate_buckets) > _RATE_GC_THRESHOLD:
                _rate_gc_locked(now)
    except Exception:
        # 限速失败时放行（避免限速器本身成为故障点）
        return True
    _persist_rate_buckets()
    return True


def _persist_rate_buckets(force: bool = False):
    """把内存窗口快照落库（节流 + 非阻塞：抢不到锁或不到间隔就跳过）。"""
    global _rate_last_persist
    now = time.time()
    if not force and now - _rate_last_persist < _RATE_PERSIST_INTERVAL:
        return
    if not _rate_persist_lock.acquire(blocking=False):
        return
    try:
        with _rate_lock:
            _rate_last_persist = now
            if not _rate_buckets:
                return
            snapshot = {k: list(v) for k, v in _rate_buckets.items()}
        conn = _get_db()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO rate_limits (bucket_key, timestamps, updated_at) VALUES (?,?,?)",
                [(k, json.dumps(v), now) for k, v in snapshot.items()],
            )
            conn.execute("DELETE FROM rate_limits WHERE updated_at < ?", (now - 3600,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("rate_limits 落库失败（不影响限速本身）", exc_info=True)
    finally:
        _rate_persist_lock.release()


def _load_rate_buckets():
    """启动时把未过期窗口载入内存（服务重启不重置限速计数）。"""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT bucket_key, timestamps FROM rate_limits WHERE updated_at >= ?",
                (now - 3600,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                ts = [t for t in json.loads(row["timestamps"]) if t > cutoff]
            except (ValueError, TypeError):
                continue
            if ts:
                _rate_buckets[row["bucket_key"]] = ts
        if _rate_buckets:
            log.info("限速窗口已载入：%d 个活跃桶", len(_rate_buckets))
    except Exception:
        log.debug("限速窗口载入失败（从零开始计数）", exc_info=True)


_load_rate_buckets()


def _rate_limit_check(ip: str, device_id: str) -> bool:
    """IP + 设备双维度限速。

    设备维度仅在客户端提供了有效签名时生效（签名无效/未提供时
    _get_device 已签发新设备，但这里用原始值判断，避免无签名请求
    每次换新设备绕过设备限速）。
    """
    if not _check_rate_limit(f"ip:{ip}", RATE_IP_PER_MINUTE):
        return False
    if device_id and not _check_rate_limit(f"dev:{device_id}", RATE_DEVICE_PER_MINUTE):
        return False
    return True


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


# ------------------------------------------------------- 2.0 前端产物（vite）
# frontend/ 的构建输出。文件名带内容 hash → 可以长缓存；Flask 侧补 gzip。
_ASSET_DIR = BASE_DIR / "app" / "static" / "dist" / "assets"
_ASSET_CACHE: dict = {}  # rel -> (raw, gz, ctype)
_ASSET_CACHE_MAX = 64


def _v2_index_ready() -> bool:
    """v2 产物是否就绪；没跑过 build 时回退 v1，保证 clone 下来直接能跑。"""
    return (BASE_DIR / "app" / "static" / "dist" / "index.html").is_file()


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


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"success": True, "status": "ok", "time": datetime.now().isoformat(), "uptime_seconds": round(time.time() - SERVICE_STARTED_AT)})


@app.route("/api/like", methods=["POST"])
def api_like():
    """单链接真实点赞查询（标准 JSON，供自动化流程/飞书工作流调用）。

    与 /api/extract 的 NDJSON 流式不同，本接口固定返回标准 JSON，
    只提取 like_count 等核心字段，便于下游系统直接解析。
    请求体：{"url": "https://..."}，支持传入单个链接或混合文本。
    """
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    # 无有效设备签名时跳过设备限速（防伪造设备每次换新绕过），仅靠 IP 限速兜底
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    data = request.get_json(silent=True) or {}
    raw = data.get("url", "") or data.get("urls", "") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    raw = str(raw).strip()
    if not raw:
        return jsonify({"success": False, "error": "请提供视频链接"}), 400

    started_at = time.monotonic()
    result = extract_link(raw)
    if not result.success:
        return jsonify({
            "success": False,
            "platform": result.platform_raw or "",
            "error": result.error or "提取失败",
            "hint": result.hint or "",
            "fetch_ms": round((time.monotonic() - started_at) * 1000),
        }), 200

    return jsonify({
        "success": True,
        "like_count": result.like_count,
        "platform": result.platform,
        "platform_raw": result.platform_raw,
        "post_id": result.post_id,
        "canonical_url": result.canonical_url,
        "author_name": result.author_name,
        "publish_time": result.publish_time,
        "fetch_ms": round((time.monotonic() - started_at) * 1000),
    })


@app.route("/api/extract", methods=["POST"])
def api_extract():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    # 无有效设备签名时跳过设备限速（防伪造设备每次换新绕过），仅靠 IP 限速兜底
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    data = request.get_json(silent=True) or {}
    raw = data.get("urls", "")
    # 压测/诊断可显式关闭历史落库，避免把公开样本混入用户转换记录。
    # 未传该字段时仍按原行为保存，保持现有前端和统计逻辑不变。
    record_history = data.get("record_history") is not False

    # 归一化为行列表：兼容字符串（含换行）和数组
    if isinstance(raw, str):
        lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    elif isinstance(raw, list):
        lines = [str(u).strip() for u in raw if str(u).strip()]
    else:
        lines = []

    # 从每行中提取所有 URL（兼容整段混合文本：文案+多个链接同一行）
    # 分享文案常在链接末尾附带中文标点，提取前统一清掉，并保持首次出现顺序去重。
    urls = []
    seen_urls = set()
    for line in lines:
        found = re.findall(r"https?://[^\s\u4e00-\u9fff]+", line)
        candidates = found or [line]  # 无 URL 的行保留原样，由提取器给出友好错误
        for candidate in candidates:
            normalized = candidate.rstrip(".,;:!?，。；：！？）】》")
            if normalized and normalized not in seen_urls:
                urls.append(normalized)
                seen_urls.add(normalized)

    if not urls:
        return jsonify({"success": False, "error": "请粘贴至少一个链接"}), 400
    if len(urls) > 20:
        return jsonify({"success": False, "error": "一次最多提取 20 条链接"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _save_to_db(item: dict) -> None:
        """单条结果入库（并发线程各自连接）。"""
        c = _get_db()
        try:
            c.execute(
                """
                INSERT INTO history
                (device_id, original_url, canonical_url, platform, title, caption,
                 author_name, publish_time, like_count, video_url, cover_url, status, error, duration_ms, cache_hit, outcome_class,
                 error_kind, retry_count, success_attempt, retry_reason, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    device_id, item["original_url"], item["canonical_url"], item["platform"],
                    item["title"], item["caption"], item["author_name"], item["publish_time"],
                    item["like_count"], item["video_url"], item["cover_url"],
                    "success" if item["success"] else "error",
                    item["error"] if not item["success"] else "",
                    item["duration_ms"], int(item["cache_hit"]), item["outcome_class"],
                    item.get("error_kind", "") or "",
                    int((item.get("telemetry") or {}).get("retry_count") or 0),
                    int((item.get("telemetry") or {}).get("success_attempt") or 0),
                    str((item.get("telemetry") or {}).get("retry_reason") or ""),
                    now,
                ),
            )
            c.commit()
        finally:
            c.close()

    def _process_one(url: str) -> dict:
        """提取单条链接（带随机抖动，避免节奏规律触发风控）。"""
        started_at = time.monotonic()
        time.sleep(random.uniform(0, 0.5))
        result = extract_link(url)
        item = {
            "original_url": url,
            "success": result.success,
            "platform": result.platform,
            "platform_raw": result.platform_raw,
            "title": result.title,
            "caption": result.caption,
            "author_name": result.author_name,
            "publish_time": result.publish_time,
            "like_count": result.like_count,
            "video_url": result.video_url,
            "canonical_url": result.canonical_url,
            "cover_url": result.cover_url,
            "post_id": result.post_id,
            "error": result.error,
            "hint": result.hint,
            "partial": result.partial,
            "cache_hit": result.cache_hit,
            "telemetry": result.telemetry,
            "error_kind": result.error_kind,
        }
        # 日志
        elapsed_ms = (time.monotonic() - started_at) * 1000
        item["duration_ms"] = round(elapsed_ms)
        if item["telemetry"]:
            log.info("请求性能分解 [%s] %s", result.platform_raw or "unknown", " ".join(f"{k}={v}ms" for k, v in item["telemetry"].items() if k.endswith("_ms")))
        item["outcome_class"] = _classify_outcome(result.success, result.error, result.error_kind)
        if result.success:
            log.info("提取成功%s [%s] %s -> %s (作者:%s 点赞:%d 耗时:%.0fms)",
                     "(仅转换)" if item.get("partial") else "",
                     result.platform, url[:60], result.canonical_url,
                     result.author_name or "-", result.like_count, elapsed_ms)
        else:
            log.warning("提取失败 [%s] 错误:%s (耗时:%.0fms)", url[:60], result.error, elapsed_ms)
        if record_history:
            _save_to_db(item)
        return item

    def _stream():
        """并发提取，每条完成立即 yield（ndjson），前端逐条渲染。"""
        batch_started_at = time.monotonic()
        # 先发头帧（携带设备标识，前端保存）
        payload = {"type": "start", "total": len(urls)}
        payload.update(_device_cookie_payload(device_id, device_sig))
        yield json.dumps(payload, ensure_ascii=False) + "\n"
        done = 0
        success_count = 0
        url_iter = iter(enumerate(urls))
        futures = {}
        batch_concurrency = _effective_extract_concurrency()
        for _ in range(min(batch_concurrency, len(urls))):
            source_index, url = next(url_iter)
            futures[_extract_queue.submit(_process_one, url)] = (source_index, url)
        while futures:
            ready, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in ready:
                source_index, url = futures[fut]
                del futures[fut]
                try:
                    item = fut.result()
                except Exception as e:
                    log.exception("并发提取异常: %s", url[:60])
                    item = {
                        "original_url": url, "success": False, "platform": "", "platform_raw": "",
                        "title": "", "caption": "", "author_name": "", "publish_time": "",
                        "like_count": 0, "video_url": "", "canonical_url": "", "cover_url": "",
                        "post_id": "", "error": "提取失败，请稍后重试", "hint": "",
                        "partial": False, "error_kind": "internal_error",
                        "outcome_class": "internal_error",
                    }
                done += 1
                success_count += int(item["success"])
                yield json.dumps(
                    {"type": "item", "index": done, "source_index": source_index, "data": item},
                    ensure_ascii=False,
                ) + "\n"
                try:
                    next_index, next_url = next(url_iter)
                    futures[_extract_queue.submit(_process_one, next_url)] = (next_index, next_url)
                except StopIteration:
                    pass
        # 收尾：仅在本次确实写入历史时清理该设备超限记录。
        if record_history:
            try:
                c = _get_db()
                c.execute(
                    """
                    DELETE FROM history WHERE device_id = ? AND id NOT IN (
                        SELECT id FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 200
                    )
                    """,
                    (device_id, device_id),
                )
                c.commit()
                c.close()
            except Exception:
                pass
        log.info(
            "批量提取完成: total=%d success=%d concurrency=%d elapsed=%.0fms",
            len(urls), success_count, batch_concurrency,
            (time.monotonic() - batch_started_at) * 1000,
        )
        yield json.dumps({"type": "end", "done": done}, ensure_ascii=False) + "\n"

    return Response(
        _stream(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/history", methods=["GET"])
def api_history():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 50", (device_id,)
    ).fetchall()
    conn.close()
    items = [dict(row) for row in rows]
    resp = {"success": True, "items": items}
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/history", methods=["DELETE"])
def api_history_clear():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    conn = _get_db()
    conn.execute("DELETE FROM history WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()
    resp = {"success": True}
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """统计看板：总数/成功/失败/平台分布/近7天趋势（按设备 ID 隔离）。"""
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    conn = _get_db()

    total = conn.execute(
        "SELECT COUNT(*) c FROM history WHERE device_id = ?", (device_id,)
    ).fetchone()["c"]
    ok_count = conn.execute(
        "SELECT COUNT(*) c FROM history WHERE device_id = ? AND status = 'success'",
        (device_id,),
    ).fetchone()["c"]
    fail_count = total - ok_count

    platform_rows = conn.execute(
        "SELECT platform, COUNT(*) c FROM history WHERE device_id = ? AND status = 'success' GROUP BY platform",
        (device_id,),
    ).fetchall()
    platform_dist = {}
    for row in platform_rows:
        name = {"douyin": "抖音", "xiaohongshu": "小红书"}.get(row["platform"], row["platform"] or "未知")
        platform_dist[name] = platform_dist.get(name, 0) + row["c"]
    # 近 7 天提取趋势（按 created_at 的日期分组）
    trend = []
    today = datetime.now()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM history WHERE device_id = ? AND created_at LIKE ?",
            (device_id, day_str + "%"),
        ).fetchone()["c"]
        trend.append({"date": day.strftime("%m-%d"), "count": cnt})

    conn.close()
    resp = {
        "success": True,
        "total": total,
        "ok": ok_count,
        "fail": fail_count,
        "success_rate": round(ok_count / total * 100, 1) if total else 0,
        "platform_dist": platform_dist,
        "trend": trend,
    }
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


# ---------------------------------------------------------------- 后台运营看板（管理员）

def _admin_require_rate(ip: str) -> bool:
    """管理员接口独立限速（每 IP 每分钟 30 次，看板轮询够用）。"""
    return _check_rate_limit(f"admin:{ip}", 30)


ADMIN_SESSION_TTL = 8 * 60 * 60


def _admin_session_value(timestamp: int) -> str:
    payload = str(timestamp)
    signature = hmac.new(ADMIN_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _admin_session_valid(value: str) -> bool:
    try:
        payload, signature = value.split(".", 1)
        timestamp = int(payload)
    except (AttributeError, ValueError):
        return False
    if timestamp > int(time.time()) or int(time.time()) - timestamp > ADMIN_SESSION_TTL:
        return False
    expected = _admin_session_value(timestamp).split(".", 1)[1]
    return hmac.compare_digest(signature, expected)


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    ip = request.remote_addr or "127.0.0.1"
    if not _check_rate_limit(f"admin-login:{ip}", 5):
        return jsonify({"success": False, "error": "登录尝试过于频繁，请稍后再试"}), 429
    payload = request.get_json(silent=True) or {}
    provided = str(payload.get("token") or "")
    if not ADMIN_TOKEN or not provided or not hmac.compare_digest(provided, ADMIN_TOKEN):
        log.warning("管理员登录失败 from %s", ip)
        return jsonify({"success": False, "error": "管理员口令错误"}), 401
    response = make_response(jsonify({"success": True}))
    response.set_cookie(
        "admin_session", _admin_session_value(int(time.time())),
        max_age=ADMIN_SESSION_TTL, httponly=True, secure=request.is_secure,
        samesite="Lax", path="/",
    )
    response.set_cookie("csrf_token", secrets.token_urlsafe(24), max_age=ADMIN_SESSION_TTL, httponly=False, secure=request.is_secure, samesite="Lax", path="/")
    _admin_audit("login", ip)
    return response


@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    _admin_audit("logout", request.remote_addr or "127.0.0.1")
    response = make_response(jsonify({"success": True}))
    response.delete_cookie("admin_session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return response


def _admin_window():
    """Resolve the dashboard time window and its SQLite lower bound."""
    raw = (request.args.get("range") or "7d").lower()
    days = {"24h": 1, "7d": 7, "30d": 30}.get(raw, 7)
    label = "24h" if raw == "24h" else f"{days}d"
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return label, days, since


@app.route("/api/admin/overview", methods=["GET"])
def api_admin_overview():
    """总览：总数/成功率/活跃设备/今日提取/近7天趋势/平台分布（全设备，管理员视角）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    window, days, since = _admin_window()
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at >= ?", (since,)).fetchone()["c"]
    ok_count = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at >= ? AND status = 'success'", (since,)).fetchone()["c"]
    fail_count = total - ok_count
    user_error_count = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at >= ? AND outcome_class IN ('invalid_input', 'expired_content')", (since,)).fetchone()["c"]
    service_failure_count = total - ok_count - user_error_count
    valid_total = total - user_error_count
    service_success_rate = round(ok_count / valid_total * 100, 1) if valid_total else 0
    input_validity_rate = round(valid_total / total * 100, 1) if total else 0
    active_devices = conn.execute("SELECT COUNT(DISTINCT device_id) c FROM history WHERE created_at >= ?", (since,)).fetchone()["c"]

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at LIKE ?", (today_str + "%",)).fetchone()["c"]
    active_7d = conn.execute("SELECT COUNT(DISTINCT device_id) c FROM history WHERE created_at >= ?", ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()["c"]
    active_30d = conn.execute("SELECT COUNT(DISTINCT device_id) c FROM history WHERE created_at >= ?", ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()["c"]

    platform_rows = conn.execute(
        "SELECT platform, COUNT(*) c FROM history WHERE created_at >= ? AND outcome_class = 'success' GROUP BY platform", (since,)
    ).fetchall()
    platform_dist = {}
    for row in platform_rows:
        name = {"douyin": "抖音", "xiaohongshu": "小红书"}.get(row["platform"], row["platform"] or "未知")
        platform_dist[name] = platform_dist.get(name, 0) + row["c"]

    platform_health = {}
    health_rows = conn.execute(
        "SELECT CASE "
        "WHEN platform != '' THEN platform "
        "WHEN lower(original_url) LIKE '%douyin%' OR lower(original_url) LIKE '%iesdouyin%' THEN 'douyin' "
        "WHEN lower(original_url) LIKE '%xiaohongshu%' OR lower(original_url) LIKE '%xhslink%' THEN 'xiaohongshu' "
        "ELSE '' END AS resolved_platform, COUNT(*) total, "
        "SUM(CASE WHEN outcome_class = 'success' THEN 1 ELSE 0 END) ok, "
        "SUM(CASE WHEN outcome_class IN ('invalid_input','expired_content') THEN 1 ELSE 0 END) user_errors, "
        "SUM(CASE WHEN outcome_class IN ('upstream_error','internal_error') THEN 1 ELSE 0 END) service_failures "
        "FROM history WHERE created_at >= ? GROUP BY resolved_platform", (since,)
    ).fetchall()
    for row in health_rows:
        name = {"douyin": "抖音", "xiaohongshu": "小红书"}.get(row["resolved_platform"], row["resolved_platform"] or "未知")
        total_platform = row["total"] or 0
        ok_platform = row["ok"] or 0
        user_platform = row["user_errors"] or 0
        valid_platform = total_platform - user_platform
        platform_health[name] = {
            "total": total_platform,
            "ok": ok_platform,
            "fail": total_platform - ok_platform,
            "success_rate": round(ok_platform / total_platform * 100, 1) if total_platform else 0,
            "service_success_rate": round(ok_platform / valid_platform * 100, 1) if valid_platform else 0,
            "user_errors": user_platform,
            "service_failures": row["service_failures"] or 0,
        }

    trend = []
    today = datetime.now()
    points = 1 if days == 1 else days
    for i in range(points - 1, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM history WHERE created_at LIKE ? AND created_at >= ?", (day_str + "%", since)
        ).fetchone()["c"]
        ok_cnt = conn.execute(
        "SELECT COUNT(*) c FROM history WHERE created_at LIKE ? AND created_at >= ? AND outcome_class = 'success'",
            (day_str + "%", since),
        ).fetchone()["c"]
        trend.append({
            "date": day.strftime("%m-%d"),
            "count": cnt,
            "ok": ok_cnt,
            "fail": cnt - ok_cnt,
        })
    conn.close()

    return jsonify({
        "success": True,
        "range": window,
        "total": total,
        "ok": ok_count,
        "fail": fail_count,
        "success_rate": round(ok_count / total * 100, 1) if total else 0,
        "service_success_rate": service_success_rate,
        "input_validity_rate": input_validity_rate,
        "user_error_count": user_error_count,
        "service_failure_count": service_failure_count,
        "active_devices": active_devices,
        "active_devices_7d": active_7d,
        "active_devices_30d": active_30d,
        "today_count": today_count,
        "platform_dist": platform_dist,
        "platform_health": platform_health,
        "trend": trend,
    })


@app.route("/api/admin/devices", methods=["GET"])
def api_admin_devices():
    """设备使用排行：提取数/成功率/首次与最后活跃（device_id 脱敏为前后缀）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    _, _, since = _admin_window()
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 100))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        limit, offset = 50, 0
    conn = _get_db()
    total_devices = conn.execute(
        "SELECT COUNT(DISTINCT device_id) c FROM history WHERE created_at >= ?", (since,)
    ).fetchone()["c"]
    rows = conn.execute(
        """
        SELECT device_id,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
               MIN(created_at) AS first_at,
               MAX(created_at) AS last_at
        FROM history
        WHERE created_at >= ?
        GROUP BY device_id
        ORDER BY total DESC
        LIMIT ? OFFSET ?
        """, (since, limit, offset)
    ).fetchall()
    conn.close()

    def _mask(did: str) -> str:
        if not did:
            return "未知"
        if len(did) <= 12:
            return did
        return f"{did[:4]}…{did[-4:]}"

    devices = []
    for r in rows:
        total = r["total"]
        ok = r["ok"] or 0
        devices.append({
            "device_id": _mask(r["device_id"]),
            "total": total,
            "ok": ok,
            "fail": total - ok,
            "success_rate": round(ok / total * 100, 1) if total else 0,
            "first_at": r["first_at"],
            "last_at": r["last_at"],
        })
    return jsonify({"success": True, "devices": devices, "total": total_devices, "limit": limit, "offset": offset})


@app.route("/api/admin/errors", methods=["GET"])
def api_admin_errors():
    """失败原因聚合：error 文本分组计数 TOP 30（不含具体链接内容）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    window, _, since = _admin_window()
    conn = _get_db()
    rows = conn.execute(
        """
        SELECT error,
               MAX(CASE WHEN error_kind != '' THEN error_kind END) AS kind,
               COUNT(*) c
        FROM history
        WHERE created_at >= ? AND status != 'success' AND error != ''
        GROUP BY error ORDER BY c DESC LIMIT 30
        """, (since,)
    ).fetchall()
    # 细粒度归因分布：error_kind 为空的是本列上线前的历史行（当时没有这个信息）
    kind_rows = conn.execute(
        """
        SELECT CASE WHEN error_kind != '' THEN error_kind ELSE '(未归因·历史数据)' END AS k,
               COUNT(*) c
        FROM history WHERE created_at >= ? AND status != 'success'
        GROUP BY k ORDER BY c DESC LIMIT 20
        """, (since,)
    ).fetchall()
    conn.close()
    # 带上 error_kind：前端「处理建议」按类型判定，不再靠匹配报错文案
    # （文案会随产品迭代变化，靠它做逻辑必然失配 —— v1.7.4/1.7.5 就发生过）。
    errors = [
        {"error": r["error"][:200], "count": r["c"], "error_kind": r["kind"] or ""}
        for r in rows
    ]
    error_kinds = [{"kind": r["k"], "count": r["c"]} for r in kind_rows]
    return jsonify({"success": True, "range": window, "errors": errors, "error_kinds": error_kinds})


@app.route("/api/admin/performance", methods=["GET"])
def api_admin_performance():
    """性能汇总：平均提取耗时、缓存命中和队列配置。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429
    window, _, since = _admin_window()
    conn = _get_db()
    row = conn.execute(
        "SELECT COUNT(*) total, AVG(duration_ms) avg_ms, SUM(cache_hit) cache_hits FROM history WHERE created_at >= ?", (since,)
    ).fetchone()
    durations = [r["duration_ms"] for r in conn.execute(
        "SELECT duration_ms FROM history WHERE created_at >= ? AND duration_ms > 0 ORDER BY duration_ms", (since,)
    ).fetchall()]
    total = row["total"] or 0
    hits = row["cache_hits"] or 0
    p95 = durations[max(0, (len(durations) * 95 + 99) // 100 - 1)] if durations else 0
    current_ok = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at >= ? AND outcome_class = 'success'", (since,)).fetchone()["c"]
    user_errors = conn.execute("SELECT COUNT(*) c FROM history WHERE created_at >= ? AND outcome_class IN ('invalid_input','expired_content')", (since,)).fetchone()["c"]
    valid_total = total - user_errors
    service_failures = valid_total - current_ok
    retry_rows = conn.execute(
        "SELECT success_attempt, COUNT(*) c FROM history WHERE created_at >= ? AND platform IN ('抖音', 'douyin') AND success_attempt > 0 GROUP BY success_attempt",
        (since,),
    ).fetchall()
    douyin_total = conn.execute(
        "SELECT COUNT(*) c FROM history WHERE created_at >= ? AND platform IN ('抖音', 'douyin')", (since,)
    ).fetchone()["c"] or 0
    retry_success_attempts = {
        str(r["success_attempt"]): {
            "count": r["c"],
            "rate": round(r["c"] / douyin_total * 100, 1) if douyin_total else 0,
        }
        for r in retry_rows
    }
    retry_reason_rows = conn.execute(
        "SELECT retry_reason, COUNT(*) c FROM history WHERE created_at >= ? AND platform IN ('抖音', 'douyin') AND retry_reason != '' GROUP BY retry_reason ORDER BY c DESC",
        (since,),
    ).fetchall()
    retry_reasons = [{"reason": r["retry_reason"], "count": r["c"]} for r in retry_reason_rows]
    alert_kind = "success_rate" if valid_total and (service_failures / valid_total) > 0.1 else ("p95" if p95 > 5000 else "")
    if alert_kind:
        message = "成功率低于 90%" if alert_kind == "success_rate" else "P95 耗时超过 5 秒"
        recent = conn.execute("SELECT 1 FROM admin_alerts WHERE kind = ? AND created_at >= ? LIMIT 1", (alert_kind, (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
        if not recent:
            conn.execute("INSERT INTO admin_alerts(kind, message, value, created_at) VALUES (?,?,?,?)", (alert_kind, message, float(p95 if alert_kind == "p95" else 0), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
    conn.close()
    return jsonify({
        "success": True,
        "range": window,
        "total": total,
        "avg_duration_ms": round(row["avg_ms"] or 0),
        "p95_duration_ms": round(p95),
        "cache_hits": hits,
        "cache_hit_rate": round(hits / total * 100, 1) if total else 0,
        "batch_concurrency": _effective_extract_concurrency(),
        "queue_concurrency": GLOBAL_EXTRACT_CONCURRENCY,
        "douyin_retry_success_attempts": retry_success_attempts,
        "douyin_retry_reasons": retry_reasons,
    })


@app.route("/api/admin/retry_hot", methods=["GET"])
def api_admin_retry_hot():
    """返回窗口内重复提交较多的链接，供运营看板定位重试热点。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429
    _window, _days, since = _admin_window()
    conn = _get_db()
    rows = conn.execute(
        """SELECT original_url AS url, MAX(platform) AS platform, COUNT(*) AS count,
                  SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS fail,
                  MAX(created_at) AS last_at
           FROM history WHERE created_at >= ? GROUP BY original_url
           HAVING COUNT(*) > 1 ORDER BY count DESC, last_at DESC LIMIT 50""",
        (since,),
    ).fetchall()
    conn.close()
    return jsonify({"success": True, "items": [dict(row) for row in rows]})

@app.route("/api/admin/recent", methods=["GET"])
def api_admin_recent():
    """最近动态（脱敏）：仅平台/状态/时间/错误摘要，不返回任何 URL 与文案。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    _, _, since = _admin_window()
    conn = _get_db()
    rows = conn.execute(
        """
        SELECT platform, status, error, created_at FROM history
        WHERE created_at >= ?
        ORDER BY id DESC LIMIT 50
        """, (since,)
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "platform": r["platform"],
            "status": r["status"],
            "error": (r["error"] or "")[:120],
            "created_at": r["created_at"],
        })
    return jsonify({"success": True, "items": items})


@app.route("/api/admin/alerts", methods=["GET"])
def api_admin_alerts():
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429
    conn = _get_db()
    rows = conn.execute("SELECT kind, message, value, created_at FROM admin_alerts ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return jsonify({"success": True, "items": [dict(r) for r in rows]})


@app.route("/api/admin/audit", methods=["GET"])
def api_admin_audit():
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429
    conn = _get_db()
    rows = conn.execute("SELECT action, ip, created_at FROM admin_audit ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"success": True, "items": [dict(r) for r in rows]})


@app.route("/api/admin/export.csv", methods=["GET"])
def api_admin_export_csv():
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429
    _, _, since = _admin_window()
    conn = _get_db()
    rows = conn.execute("SELECT platform, status, outcome_class, error_kind, error, duration_ms, cache_hit, created_at FROM history WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
    conn.close()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["platform", "status", "outcome_class", "error_kind", "error", "duration_ms", "cache_hit", "created_at"])
    writer.writerows([tuple(r) for r in rows])
    response = make_response(out.getvalue().encode("utf-8-sig"))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=link-extractor-report.csv"
    return response


# ---------------------------------------------------------------- 代理池运维（管理员）

POOL_BIN = os.environ.get("AJIASU_POOL_BIN", "/opt/ajiasu-pool/pool.py")
POOL_PYTHON = os.environ.get("AJIASU_POOL_PYTHON", "/usr/bin/python3")
POOL_CONFIG_PATH = Path(os.environ.get("AJIASU_POOL_CONFIG", "/opt/ajiasu-pool/pool-config.json"))
POOL_LOG_PATH = Path("/var/log/ajiasu-pool.log")
POOL_CMD_TIMEOUT = 180
POOL_STATUS_TIMEOUT = 30
# 历史行兼容用：老数据的 error_kind 是空串，只能靠文案匹配认出风控失败；
# 新数据（v1.7.x 起）有 error_kind，走类型判定。两者在 _risk_trend 里是 OR 关系。
# ⚠️ 正因为留着这段兼容，用户可见的报错文案不能随手改 —— 改前先确认
#    lib/extractor.py 的 `_is_risk_control_error`（同样留了一处文案兜底）已解耦。
_RISK_ERROR_MARKER = "平台暂时限制"


def _parse_pool_json(raw):
    """从代理池 CLI 输出里取出 JSON 对象。

    pool.py 的 --json 有两种形态：`status` 用缩进多行、其余多为单行。
    所以不能只按"单行 JSON"解析（曾经因此把 status 的整包 payload 丢掉，
    面板只看到 success/error 两个键）。这里做三级回退：
    整体解析 → 从最后一个行首为 '{' 的位置吃掉剩余内容 → 放弃。
    """
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    lines = (raw or "").splitlines()
    decoder = json.JSONDecoder()
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith("{"):
            continue
        try:
            obj, _end = decoder.raw_decode("\n".join(lines[i:]))
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _pool_run(args, stdin_text="", timeout=POOL_CMD_TIMEOUT):
    """调用代理池 CLI，取其中的 JSON 结果。

    stdin_text 配合 pool.py 的 --pass-stdin 使用：密码走管道而不是命令行，
    因此不会出现在 ps / 进程命令行里。
    """
    try:
        proc = subprocess.run(
            [POOL_PYTHON, POOL_BIN, *args],
            input=stdin_text or None,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, {"success": False, "error": f"代理池调用失败：{exc}"}, ""
    raw = proc.stdout or ""
    payload = _parse_pool_json(raw)
    if payload is None:
        tail = raw.strip()[-400:]
        return proc.returncode == 0, {
            "success": proc.returncode == 0,
            "error": "" if proc.returncode == 0 else (tail or "代理池未返回结果"),
        }, raw
    payload.setdefault("success", bool(payload.get("ok")))
    return proc.returncode == 0, payload, raw


def _read_pool_config() -> dict:
    try:
        data = json.loads(POOL_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _pool_recent_events(limit: int = 8) -> list:
    """代理池日志里最近的事件（只留动作与出口 IP；日志本身不含密码）。"""
    if not POOL_LOG_PATH.exists():
        return []
    try:
        lines = POOL_LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        if "[pool]" not in line:
            continue
        body = line.split("[pool]", 1)[-1].strip()
        if body.startswith(("建立租约", "释放租约", "换IP兜底开关")):
            events.append({"time": line[:19], "text": body[:120]})
            if len(events) >= limit:
                break
    return events


def _risk_trend(days: int = 7) -> list:
    """近 N 天平台风控失败趋势。

    直接统计历史库，与兜底开关无关——所以「关上兜底」也能继续看到趋势，
    这样判断「要不要再打开」靠数据而不是记性。
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _get_db()
    try:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS fail,
                   SUM(CASE WHEN error_kind IN ('platform_limited', 'short_link_blocked')
                             OR error LIKE ? THEN 1 ELSE 0 END) AS risk
            FROM history WHERE created_at >= ?
            GROUP BY day ORDER BY day
            """,
            (f"%{_RISK_ERROR_MARKER}%", since),
        ).fetchall()
    finally:
        conn.close()
    return [{
        "day": r["day"],
        "total": r["total"] or 0,
        "fail": r["fail"] or 0,
        "risk": r["risk"] or 0,
    } for r in rows]


def _pool_precheck():
    """代理池接口统一前置限速。返回 (ip, error_response)。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return ip, (jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429)
    return ip, None


@app.route("/api/admin/pool/status", methods=["GET"])
def api_admin_pool_status():
    """代理池总览：账号额度记账 + 运行时状态 + 兜底开关 + 风控趋势 + 最近事件。"""
    _ip, err = _pool_precheck()
    if err:
        return err
    ok, payload, _raw = _pool_run(["status", "--json"], timeout=POOL_STATUS_TIMEOUT)
    return jsonify({
        "success": True,
        "poolAvailable": ok,
        "pool": payload,
        "config": _read_pool_config(),
        "recentEvents": _pool_recent_events(),
        "riskTrend": _risk_trend(7),
    })


@app.route("/api/admin/pool/failover", methods=["POST"])
def api_admin_pool_failover():
    """热切换 5003 换 IP 兜底开关：写 pool-config.json，立刻生效、不重启 5003。"""
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"success": False, "error": "enabled 必须是布尔值"}), 400
    action = "on" if enabled else "off"
    ok, payload, _raw = _pool_run(["failover", action, "--json"], timeout=20)
    _admin_audit(f"pool-failover-{action}", ip)
    if not ok or payload.get("failoverEnabled") != enabled:
        return jsonify({"success": False, "error": payload.get("error") or "切换失败"}), 500
    return jsonify({"success": True, "failoverEnabled": enabled})


@app.route("/api/admin/pool/account", methods=["POST"])
def api_admin_pool_add_account():
    """新增爱加速账号。

    密码只经由 POST body → 子进程 stdin，不写日志、不进进程命令行；
    配置文件由 pool.py 以 0600 落盘。
    """
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    user = str(body.get("user") or "").strip()
    password = str(body.get("password") or "")
    if not user or not password:
        return jsonify({"success": False, "error": "手机号和密码都不能为空"}), 400
    try:
        daily = max(0, min(int(body.get("dailySeconds") or 1200), 24 * 3600))
        reserve = max(0, min(int(body.get("reserveSeconds") or 0), 24 * 3600))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "额度必须是整数秒"}), 400
    args = ["add-account", "--user", user, "--pass-stdin",
            "--daily-seconds", str(daily), "--reserve-seconds", str(reserve)]
    if name:
        args += ["--name", name]
    note = str(body.get("note") or "").strip()
    if note:
        args += ["--note", note]
    ok, payload, _raw = _pool_run(args, stdin_text=password + "\n")
    final_name = str(payload.get("name") or name or user)
    _admin_audit(f"pool-add-account:{final_name}", ip)
    if not ok:
        return jsonify({"success": False, "error": payload.get("message") or payload.get("error") or "新增失败，请核对手机号与密码"}), 400
    return jsonify({"success": True, "name": final_name})


@app.route("/api/admin/pool/account/remove", methods=["POST"])
def api_admin_pool_remove_account():
    """从池中移除账号（可选同时删除其配置文件）。"""
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    if not name:
        return jsonify({"success": False, "error": "缺少账号名"}), 400
    args = ["remove-account", "--name", name]
    if body.get("deleteConfig"):
        args.append("--delete-config")
    ok, payload, _raw = _pool_run(args, timeout=30)
    _admin_audit(f"pool-remove-account:{name}", ip)
    if not ok:
        return jsonify({"success": False, "error": payload.get("error") or "移除失败"}), 400
    return jsonify({"success": True})


@app.route("/api/admin/pool/account/update", methods=["POST"])
def api_admin_pool_update_account():
    """改账号非敏感项（启停 / 额度 / 预留 / 备注）。换手机号走新增，改密码走重置。"""
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    if not name:
        return jsonify({"success": False, "error": "缺少账号名"}), 400
    args = ["update-account", "--name", name]
    changed = []
    if isinstance(body.get("enabled"), bool):
        args += ["--enabled", "true" if body["enabled"] else "false"]
        changed.append("enabled")
    for key, flag in (("dailySeconds", "--daily-seconds"), ("reserveSeconds", "--reserve-seconds")):
        if body.get(key) is not None:
            try:
                args += [flag, str(max(0, min(int(body[key]), 24 * 3600)))]
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": f"{key} 必须是整数秒"}), 400
            changed.append(key)
    if body.get("note") is not None:
        args += ["--note", str(body["note"])[:120]]
        changed.append("note")
    if not changed:
        return jsonify({"success": False, "error": "没有需要修改的字段"}), 400
    ok, payload, _raw = _pool_run(args, timeout=30)
    _admin_audit(f"pool-update-account:{name}:{','.join(changed)}", ip)
    if not ok:
        return jsonify({"success": False, "error": payload.get("error") or "更新失败"}), 400
    return jsonify({"success": True, "account": payload.get("account")})


@app.route("/api/admin/pool/account/password", methods=["POST"])
def api_admin_pool_set_password():
    """重置账号密码：新密码同样走 stdin，并立刻验证登录是否通过。"""
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    password = str(body.get("password") or "")
    if not name or not password:
        return jsonify({"success": False, "error": "账号名与新密码都不能为空"}), 400
    ok, payload, _raw = _pool_run(
        ["set-password", "--name", name, "--pass-stdin"], stdin_text=password + "\n", timeout=90
    )
    _admin_audit(f"pool-set-password:{name}", ip)
    return jsonify({
        "success": bool(ok and payload.get("ok")),
        "message": payload.get("message") or payload.get("error") or ("密码已更新" if ok else "重置失败"),
    })


@app.route("/api/admin/pool/release", methods=["POST"])
def api_admin_pool_release():
    """手动释放当前租约（会动 1080；被签到占用时 pool 侧会拒绝而不抢占）。"""
    ip, err = _pool_precheck()
    if err:
        return err
    ok, payload, _raw = _pool_run(["release", "--json"], timeout=30)
    _admin_audit("pool-release", ip)
    if not ok:
        return jsonify({"success": False, "error": payload.get("error") or "释放失败"}), 400
    return jsonify({"success": True, "result": payload})


@app.route("/api/admin/pool/reap", methods=["POST"])
def api_admin_pool_reap():
    """清理超时/僵死租约（会动 1080）。"""
    ip, err = _pool_precheck()
    if err:
        return err
    ok, payload, _raw = _pool_run(["reap", "--json"], timeout=45)
    _admin_audit("pool-reap", ip)
    if not ok:
        return jsonify({"success": False, "error": payload.get("error") or "回收失败"}), 400
    return jsonify({"success": True, "result": payload})


@app.route("/api/admin/pool/check", methods=["POST"])
def api_admin_pool_check():
    """轻检：验证账号登录 + 数免费节点。不占 1080、不耗额度，适合随时点。"""
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    args = ["check"] + (["--name", name] if name else [])
    ok, _payload, raw = _pool_run(args, timeout=POOL_CMD_TIMEOUT)
    _admin_audit(f"pool-check:{name or 'all'}", ip)
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    return jsonify({"success": True, "ok": ok, "lines": lines[-12:]})


@app.route("/api/admin/pool/deepcheck", methods=["POST"])
def api_admin_pool_deepcheck():
    """深度体检：真租一个出口 IP、与直连对照、并用它请求一次小红书。

    占 1080 几秒并消耗少量额度，所以做成独立按钮，不跟着列表轮询跑。
    """
    ip, err = _pool_precheck()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name") or ""))
    args = ["deepcheck"] + (["--name", name] if name else [])
    _ok, payload, _raw = _pool_run(args, timeout=POOL_CMD_TIMEOUT)
    _admin_audit(f"pool-deepcheck:{name or 'auto'}", ip)
    return jsonify({
        "success": True,
        "passed": bool(payload.get("ok")),
        "result": payload,
    })


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
    """后台运营看板页面（鉴权由前端 + /api/admin/* 双重保障）。"""
    return send_from_directory("app/templates", "admin.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5003"))
    log.info("=" * 56)
    log.info("链接提取工具启动中...")
    log.info("访问地址: http://127.0.0.1:%d", port)
    log.info("按 Ctrl+C 停止服务")
    log.info("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
