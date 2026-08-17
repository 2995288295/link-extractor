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
import os
import random
import re
import secrets
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

import requests as _requests
from flask import Flask, Response, jsonify, request, send_from_directory

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
    if not ADMIN_TOKEN:
        return jsonify({"success": False, "error": "管理员口令未配置"}), 503
    provided = request.headers.get("X-Admin-Token", "")
    if not provided or not hmac.compare_digest(provided, ADMIN_TOKEN):
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
    conn.commit()
    conn.close()


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
    return jsonify({"success": True, "status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    # 无有效设备签名时跳过设备限速（防伪造设备每次换新绕过），仅靠 IP 限速兜底
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    data = request.get_json(silent=True) or {}
    raw = data.get("urls", "")

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
                 author_name, publish_time, like_count, video_url, cover_url, status, error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    device_id, item["original_url"], item["canonical_url"], item["platform"],
                    item["title"], item["caption"], item["author_name"], item["publish_time"],
                    item["like_count"], item["video_url"], item["cover_url"],
                    "success" if item["success"] else "error",
                    item["error"] if not item["success"] else "",
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
        }
        # 日志
        elapsed_ms = (time.monotonic() - started_at) * 1000
        if result.success:
            log.info("提取成功 [%s] %s -> %s (作者:%s 点赞:%d 耗时:%.0fms)",
                     result.platform, url[:60], result.canonical_url,
                     result.author_name or "-", result.like_count, elapsed_ms)
        else:
            log.warning("提取失败 [%s] 错误:%s (耗时:%.0fms)", url[:60], result.error, elapsed_ms)
        # 入库
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
        with ThreadPoolExecutor(max_workers=EXTRACT_CONCURRENCY) as pool:
            futures = {pool.submit(_process_one, u): (source_index, u) for source_index, u in enumerate(urls)}
            for fut in as_completed(futures):
                source_index, url = futures[fut]
                try:
                    item = fut.result()
                except Exception as e:
                    log.exception("并发提取异常: %s", url[:60])
                    item = {
                        "original_url": url, "success": False, "platform": "", "platform_raw": "",
                        "title": "", "caption": "", "author_name": "", "publish_time": "",
                        "like_count": 0, "video_url": "", "canonical_url": "", "cover_url": "",
                        "post_id": "", "error": "提取失败，请稍后重试", "hint": "",
                    }
                done += 1
                success_count += int(item["success"])
                yield json.dumps(
                    {"type": "item", "index": done, "source_index": source_index, "data": item},
                    ensure_ascii=False,
                ) + "\n"
        # 收尾：清理该设备超限历史
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
            len(urls), success_count, EXTRACT_CONCURRENCY,
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


@app.route("/api/admin/overview", methods=["GET"])
def api_admin_overview():
    """总览：总数/成功率/活跃设备/今日提取/近7天趋势/平台分布（全设备，管理员视角）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) c FROM history").fetchone()["c"]
    ok_count = conn.execute("SELECT COUNT(*) c FROM history WHERE status = 'success'").fetchone()["c"]
    fail_count = total - ok_count
    active_devices = conn.execute("SELECT COUNT(DISTINCT device_id) c FROM history").fetchone()["c"]

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) c FROM history WHERE created_at LIKE ?", (today_str + "%",)
    ).fetchone()["c"]

    platform_rows = conn.execute(
        "SELECT platform, COUNT(*) c FROM history WHERE status = 'success' GROUP BY platform"
    ).fetchall()
    platform_dist = {}
    for row in platform_rows:
        name = {"douyin": "抖音", "xiaohongshu": "小红书"}.get(row["platform"], row["platform"] or "未知")
        platform_dist[name] = platform_dist.get(name, 0) + row["c"]

    trend = []
    today = datetime.now()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM history WHERE created_at LIKE ?", (day_str + "%",)
        ).fetchone()["c"]
        ok_cnt = conn.execute(
            "SELECT COUNT(*) c FROM history WHERE created_at LIKE ? AND status = 'success'",
            (day_str + "%",),
        ).fetchone()["c"]
        trend.append({
            "date": day.strftime("%m-%d"),
            "count": cnt,
            "ok": ok_cnt,
        })
    conn.close()

    return jsonify({
        "success": True,
        "total": total,
        "ok": ok_count,
        "fail": fail_count,
        "success_rate": round(ok_count / total * 100, 1) if total else 0,
        "active_devices": active_devices,
        "today_count": today_count,
        "platform_dist": platform_dist,
        "trend": trend,
    })


@app.route("/api/admin/devices", methods=["GET"])
def api_admin_devices():
    """设备使用排行：提取数/成功率/首次与最后活跃（device_id 脱敏为前后缀）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    conn = _get_db()
    rows = conn.execute(
        """
        SELECT device_id,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
               MIN(created_at) AS first_at,
               MAX(created_at) AS last_at
        FROM history
        GROUP BY device_id
        ORDER BY total DESC
        LIMIT 100
        """
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
    return jsonify({"success": True, "devices": devices})


@app.route("/api/admin/errors", methods=["GET"])
def api_admin_errors():
    """失败原因聚合：error 文本分组计数 TOP 30（不含具体链接内容）。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    conn = _get_db()
    rows = conn.execute(
        """
        SELECT error, COUNT(*) c FROM history
        WHERE status != 'success' AND error != ''
        GROUP BY error ORDER BY c DESC LIMIT 30
        """
    ).fetchall()
    conn.close()
    errors = [{"error": r["error"][:200], "count": r["c"]} for r in rows]
    return jsonify({"success": True, "errors": errors})


@app.route("/api/admin/recent", methods=["GET"])
def api_admin_recent():
    """最近动态（脱敏）：仅平台/状态/时间/错误摘要，不返回任何 URL 与文案。"""
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    conn = _get_db()
    rows = conn.execute(
        """
        SELECT platform, status, error, created_at FROM history
        ORDER BY id DESC LIMIT 50
        """
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
