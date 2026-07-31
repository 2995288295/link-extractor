"""链接提取工具 - Flask 后端

功能：
- POST /api/extract   批量提取（多行链接 → 每条独立结果）
- GET/POST /api/history  历史记录（按设备 ID 隔离）
- GET /api/health    健康检查
- GET /              前端页面

部署：venv + gunicorn，端口 5002
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from lib.extractor import extract_link

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

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "history.db"

app = Flask(__name__, static_folder="app/static", static_url_path="/static")
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # 请求体 512KB

# ---------------------------------------------------------------- 请求日志

@app.before_request
def _log_request_start():
    if request.path.startswith("/api/"):
        request._start_time = time.time()
        log.info(">>> [%s] %s from %s", request.method, request.path, request.remote_addr)


@app.after_request
def _log_request_end(response):
    if request.path.startswith("/api/"):
        log.info("<<< [%s] %s -> %d (%.0fms)",
                 request.method, request.path, response.status_code,
                 (time.time() - request._start_time) * 1000 if hasattr(request, "_start_time") else 0)
    return response

# ---------------------------------------------------------------- 数据库

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_device ON history(device_id, created_at)")
    conn.commit()
    conn.close()


_init_db()

# ---------------------------------------------------------------- 设备 ID

def _get_device_id(req) -> str:
    """从 X-Device-Id 头或 cookie 获取设备 ID，不存在则生成新 ID。"""
    device_id = req.headers.get("X-Device-Id", "")
    if not device_id:
        device_id = req.cookies.get("device_id", "")
    if device_id and len(device_id) <= 64:
        return device_id
    return str(uuid.uuid4())


# ---------------------------------------------------------------- 速率限制

_rate_limits: dict[str, list[float]] = {}
MAX_REQUESTS_PER_MINUTE = 30


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    bucket = _rate_limits.setdefault(ip, [])
    bucket[:] = [t for t in bucket if now - t < 60]
    if len(bucket) >= MAX_REQUESTS_PER_MINUTE:
        return False
    bucket.append(now)
    return True


# ---------------------------------------------------------------- API

@app.after_request
def _set_device_cookie(response):
    """响应时把设备 ID 写进 cookie（所有 API 方法），前端无需手动处理。"""
    if request.path.startswith("/api/"):
        existing = request.cookies.get("device_id")
        if not existing:
            device_id = request.headers.get("X-Device-Id", "")
            if device_id:
                response.set_cookie(
                    "device_id", device_id,
                    max_age=60 * 60 * 24 * 365, httponly=False,
                    samesite="Lax",
                )
    return response


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"success": True, "status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    ip = request.remote_addr or "127.0.0.1"
    if not _check_rate_limit(ip):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    device_id = _get_device_id(request)
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
    urls = []
    for line in lines:
        found = re.findall(r"https?://[^\s\u4e00-\u9fff]+", line)
        if found:
            urls.extend(found)
        else:
            urls.append(line)  # 无 URL 的行保留原样，由提取器给出友好错误

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
        if result.success:
            log.info("提取成功 [%s] %s -> %s (作者:%s 点赞:%d)",
                     result.platform, url[:60], result.canonical_url,
                     result.author_name or "-", result.like_count)
        else:
            log.warning("提取失败 [%s] 错误:%s", url[:60], result.error)
        # 入库
        _save_to_db(item)
        return item

    def _stream():
        """并发提取，每条完成立即 yield（ndjson），前端逐条渲染。"""
        # 先发头帧
        yield json.dumps({"type": "start", "total": len(urls), "device_id": device_id}, ensure_ascii=False) + "\n"
        done = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_process_one, u): u for u in urls}
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    item = fut.result()
                except Exception as e:
                    log.exception("并发提取异常: %s", url[:60])
                    item = {
                        "original_url": url, "success": False, "platform": "", "platform_raw": "",
                        "title": "", "caption": "", "author_name": "", "publish_time": "",
                        "like_count": 0, "video_url": "", "canonical_url": "", "cover_url": "",
                        "post_id": "", "error": f"提取异常: {e}", "hint": "请稍后重试",
                    }
                done += 1
                yield json.dumps({"type": "item", "index": done, "data": item}, ensure_ascii=False) + "\n"
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
        yield json.dumps({"type": "end", "done": done}, ensure_ascii=False) + "\n"

    from flask import Response
    return Response(
        _stream(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/history", methods=["GET"])
def api_history():
    device_id = _get_device_id(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 50", (device_id,)
    ).fetchall()
    conn.close()
    items = [dict(row) for row in rows]
    return jsonify({"success": True, "items": items, "device_id": device_id})


@app.route("/api/history", methods=["DELETE"])
def api_history_clear():
    device_id = _get_device_id(request)
    conn = _get_db()
    conn.execute("DELETE FROM history WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """统计看板：总数/成功/失败/平台分布/近7天趋势（按设备 ID 隔离）。"""
    device_id = _get_device_id(request)
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
    return jsonify({
        "success": True,
        "device_id": device_id,
        "total": total,
        "ok": ok_count,
        "fail": fail_count,
        "success_rate": round(ok_count / total * 100, 1) if total else 0,
        "platform_dist": platform_dist,
        "trend": trend,
    })


# ---------------------------------------------------------------- 封面代理

import requests as _requests
from urllib.parse import urlparse as _urlparse

_cover_cache: dict[str, tuple[bytes, str, float]] = {}
COVER_CACHE_SECONDS = 60 * 60 * 24  # 封面缓存 24 小时
_ALLOWED_IMAGE_HOSTS = (
    "douyinpic.com", "douyinimg.com", "douyinvod.com",
    "xhscdn.com", "xiaohongshu.com", "sns-img", "cn-hangzhou",
    "xhslink.com",
)


def _is_cover_url_safe(url: str) -> bool:
    """封面代理白名单：仅允许抖音/小红书的图片域名（防 SSRF）。"""
    try:
        parsed = _urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return any(d in host for d in _ALLOWED_IMAGE_HOSTS)
    except Exception:
        return False


@app.route("/api/cover")
def api_cover():
    """代理封面图片：绕开跨域防盗链 + 签名校验，带 24h 缓存。"""
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
        r = _requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"},
            timeout=15,
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.content:
            return "fetch failed", 502
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        _cover_cache[url] = (r.content, ctype, now)
        if len(_cover_cache) > 300:  # 防内存膨胀
            _cover_cache.clear()
        resp = app.response_class(r.content, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception:
        return "fetch error", 502


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return send_from_directory("app/templates", "index.html")


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
