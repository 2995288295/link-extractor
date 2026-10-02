"""后台运营 API（P5 拆包自 app.py，仅管理员）。

⚠️ 按平台统计**绝不能** `GROUP BY platform`：短链阶段就 302 的失败行 platform 是空串，
   必须回退 `original_url` 推断（见 overview 里的 CASE 写法）。曾把 35 次平台限制统计成 0。
"""

from __future__ import annotations

import csv
import hmac
import io
import secrets
import time
from datetime import datetime, timedelta

from flask import jsonify, make_response, request

from .. import app
from ..config import ADMIN_SESSION_TTL, ADMIN_SESSION_TTL_LONG, ADMIN_TOKEN, GLOBAL_EXTRACT_CONCURRENCY, _effective_extract_concurrency, log
from ..db import _admin_audit, _get_db
from ..ratelimit import _check_rate_limit
from ..security import _admin_require_rate, _admin_session_value



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
    # 勾了「记住我」→ 长会话（90 天绝对上限 + 7 天空闲失效）；否则短会话 8 小时
    remember = bool(payload.get("remember"))
    now = int(time.time())
    lifetime = ADMIN_SESSION_TTL_LONG if remember else ADMIN_SESSION_TTL
    expires = now + lifetime if remember else None      # None = 短会话（沿用旧格式）
    response = make_response(jsonify({"success": True, "remember": remember}))
    response.set_cookie(
        "admin_session", _admin_session_value(now, expires),
        max_age=lifetime, httponly=True, secure=request.is_secure,
        samesite="Lax", path="/",
    )
    response.set_cookie("csrf_token", secrets.token_urlsafe(24), max_age=lifetime, httponly=False, secure=request.is_secure, samesite="Lax", path="/")
    _admin_audit("login_remember" if remember else "login", ip)
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
