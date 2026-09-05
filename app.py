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
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import sys
import time
import uuid
import csv
import io
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

import requests as _requests
from flask import Flask, Response, jsonify, make_response, request, send_from_directory

from lib.extractor import extract_link

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
    if not provided:
        return jsonify({"success": False, "error": "管理员口令错误或未提供"}), 401
    return jsonify({"success": False, "error": "管理员口令错误或未提供"}), 401
    return None


@app.after_request
def _log_request_end(response):
    if request.path.startswith("/api/"):
        log.info("<<< [%s] %s -> %d (%.0fms)",
                 request.method, request.path, response.status_code,
                 (time.time() - request._start_time) * 1000 if hasattr(request, "_start_time") else 0)
    return response

# ---------------------------------------------------------------- 数据库

def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 模式：并发读写不互相阻塞，提升多线程写库性能
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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


def _classify_outcome(success: bool, error: str) -> str:
    """Separate user input mistakes from upstream and internal failures."""
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


# ---------------------------------------------------------------- 速率限制（SQLite 持久化）

RATE_IP_PER_MINUTE = int(os.environ.get("RATE_IP_PER_MINUTE", "20"))      # 每 IP 每分钟
RATE_DEVICE_PER_MINUTE = int(os.environ.get("RATE_DEVICE_PER_MINUTE", "15"))  # 每设备每分钟
_RATE_WINDOW = 60  # 秒
_rate_write_count = 0  # 限速写计数，用于周期性清理过期桶


def _check_rate_limit(bucket_key: str, limit: int) -> bool:
    """SQLite 持久化的滑动窗口限速；超限返回 False。"""
    global _rate_write_count
    now = time.time()
    cutoff = now - _RATE_WINDOW
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT timestamps FROM rate_limits WHERE bucket_key = ?", (bucket_key,)
        ).fetchone()
        try:
            timestamps = json.loads(row["timestamps"]) if row else []
        except (ValueError, TypeError):
            timestamps = []
        # 清理窗口外的旧时间戳
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        conn.execute(
            "INSERT OR REPLACE INTO rate_limits (bucket_key, timestamps, updated_at) VALUES (?,?,?)",
            (bucket_key, json.dumps(timestamps), now),
        )
        # 定期清理过期桶（防止表无限增长；每 ~100 次写入触发一次）
        _rate_write_count += 1
        if _rate_write_count % 100 == 0:
            conn.execute("DELETE FROM rate_limits WHERE updated_at < ?", (now - 3600,))
        conn.commit()
        return True
    except Exception:
        # 限速失败时放行（避免限速器本身成为故障点）
        return True
    finally:
        conn.close()


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
    if request.path.startswith("/static/") and "/sw.js" not in request.path:
        response.headers.setdefault("Cache-Control", "public, max-age=3600")

    # gzip 压缩（仅普通字符串响应，跳过文件响应/流式响应）
    # send_from_directory 文件响应有 Accept-Ranges 头且 body 是流，get_data() 不可用
    if (
        response.status_code == 200
        and not response.direct_passthrough
        and not response.is_streamed
        and "Accept-Ranges" not in response.headers
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
    return response


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
                 retry_count, success_attempt, retry_reason, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    device_id, item["original_url"], item["canonical_url"], item["platform"],
                    item["title"], item["caption"], item["author_name"], item["publish_time"],
                    item["like_count"], item["video_url"], item["cover_url"],
                    "success" if item["success"] else "error",
                    item["error"] if not item["success"] else "",
                    item["duration_ms"], int(item["cache_hit"]), item["outcome_class"],
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
        }
        # 日志
        elapsed_ms = (time.monotonic() - started_at) * 1000
        item["duration_ms"] = round(elapsed_ms)
        if item["telemetry"]:
            log.info("请求性能分解 [%s] %s", result.platform_raw or "unknown", " ".join(f"{k}={v}ms" for k, v in item["telemetry"].items() if k.endswith("_ms")))
        item["outcome_class"] = _classify_outcome(result.success, result.error)
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
                        "partial": False,
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
        SELECT error, COUNT(*) c FROM history
        WHERE created_at >= ? AND status != 'success' AND error != ''
        GROUP BY error ORDER BY c DESC LIMIT 30
        """, (since,)
    ).fetchall()
    conn.close()
    errors = [{"error": r["error"][:200], "count": r["c"]} for r in rows]
    return jsonify({"success": True, "range": window, "errors": errors})


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
    rows = conn.execute("SELECT platform, status, outcome_class, error, duration_ms, cache_hit, created_at FROM history WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
    conn.close()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["platform", "status", "outcome_class", "error", "duration_ms", "cache_hit", "created_at"])
    writer.writerows([tuple(r) for r in rows])
    response = make_response(out.getvalue().encode("utf-8-sig"))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=link-extractor-report.csv"
    return response


# ---------------------------------------------------------------- 封面代理

_cover_cache: dict[str, tuple[bytes, str, float]] = {}
COVER_CACHE_SECONDS = 60 * 60 * 24  # 封面缓存 24 小时
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
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                return resp
            current = _urljoin(current, location)
            continue
        return resp
    raise RuntimeError("重定向次数过多")


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
        return resp

    try:
        r = _fetch_cover_with_redirect_check(url)
        if r.status_code != 200 or not r.content:
            return "fetch failed", 502
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        _cover_cache[url] = (r.content, ctype, now)
        if len(_cover_cache) > 300:  # 防内存膨胀：淘汰最旧的一半（保留热数据）
            _stale = sorted(_cover_cache.items(), key=lambda kv: kv[1][2])[: len(_cover_cache) // 2]
            for k, _v in _stale:
                _cover_cache.pop(k, None)
        resp = app.response_class(r.content, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception:
        return "fetch error", 502


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return send_from_directory("app/templates", "index.html")


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
