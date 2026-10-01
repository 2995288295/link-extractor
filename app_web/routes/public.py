"""用户侧 API（P5 拆包自 app.py）。

`/api/extract` 是 NDJSON 流式接口：每条链接完成即 yield 一行，前端逐条渲染。
并发由 `config._extract_queue`（进程级线程池）承载；单批并发由
`_effective_extract_concurrency()` 在「月底月初保守模式」下自动降档。
"""

from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import FIRST_COMPLETED, wait
from datetime import datetime, timedelta

from flask import Response, jsonify, request
from lib.extractor import extract_link

from .. import app
from ..config import SERVICE_STARTED_AT, _effective_extract_concurrency, _extract_queue, log
from ..db import _get_db
from ..outcome import _classify_outcome
from ..ratelimit import _rate_limit_check
from ..security import _admin_require_rate, _device_cookie_payload, _get_device

# 每台设备的历史保留条数（v1.13.1 起 200 → 1000）：
# 「本月有效条数」按历史表统计，高产用户一个月可超 200 条，保留过短会静默少算。
_HISTORY_RETENTION_PER_DEVICE = 1000



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
        # 本月已提报集合（成功记录的规范链接，去 query 串）：给每条结果打
        # 「重复提报」标记用，本批次内刚提报过的也会命中。
        month_keys = _load_month_canonical_keys(device_id)
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
                        "duplicate_this_month": False,
                    }
                # 重复提报标记：先比对再登记，同批次第二条同链也会被标。
                if item.get("success") and item.get("canonical_url"):
                    dup_key = str(item["canonical_url"]).split("?", 1)[0]
                    item["duplicate_this_month"] = dup_key in month_keys
                    month_keys.add(dup_key)
                else:
                    item["duplicate_this_month"] = False
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
        # 保留上限 200 → 1000（v1.13.1）：「本月有效条数」按历史统计，
        # 高产用户一个月可超 200 条，不放宽会静默少算。
        if record_history:
            try:
                c = _get_db()
                cutoff = c.execute(
                    "SELECT id FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (device_id, _HISTORY_RETENTION_PER_DEVICE),
                ).fetchone()
                if cutoff is not None:
                    # id 单调递增：截断位及更旧的全部删除，等价于保留最新 N 条。
                    c.execute(
                        "DELETE FROM history WHERE device_id = ? AND id <= ?",
                        (device_id, cutoff["id"]),
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



# ------------------------------------------------ 成员署名（v1.14.0 · 2026-10-01）

# 身份枚举（v1.17.0）：成员自选一种，后台按身份分组看产量。
# 存 code 不存中文标签——标签调整不必刷历史数据。
_MEMBER_IDENTITIES = {
    "signed": "签约创作者",
    "ambassador": "创作大使",
    "school": "学校/区域创作者",
}


def _ensure_members_table() -> None:
    """成员署名表：device_id ↔ 真实姓名（+ 身份）。

    延迟建表（IF NOT EXISTS 幂等，重复调用无副作用），避免改 db.py 的建表序列。
    姓名只是**署名标记**（无密码，可随意填），用途是后台按人聚合产量；
    同一人换设备重填即产生新映射，后台按 name 而非 device_id 聚合。
    v1.17.0 增加 identity 列：老表用 PRAGMA 探测后 ALTER 补列（幂等）。
    """
    conn = _get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS members ("
            "device_id TEXT PRIMARY KEY, name TEXT NOT NULL, identity TEXT DEFAULT '', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(members)")}
        if "identity" not in columns:
            conn.execute("ALTER TABLE members ADD COLUMN identity TEXT DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    _ensure_members_table()
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT name, identity FROM members WHERE device_id = ?", (device_id,)
        ).fetchone()
    finally:
        conn.close()
    resp = {
        "success": True,
        "name": (row["name"] if row else ""),
        "identity": (row["identity"] if row else ""),
        "identities": [{"key": k, "label": v} for k, v in _MEMBER_IDENTITIES.items()],
    }
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/profile", methods=["POST"])
def api_profile_set():
    """登记/更新署名。无密码：姓名可冒填，定位是区分产量而非鉴权。"""
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "请输入姓名"}), 400
    if len(name) > 20:
        return jsonify({"success": False, "error": "姓名不能超过 20 个字"}), 400
    identity = str(payload.get("identity") or "").strip()
    if identity and identity not in _MEMBER_IDENTITIES:
        return jsonify({"success": False, "error": "身份选择有误，请重新选择"}), 400

    _ensure_members_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO members (device_id, name, identity, created_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET name = excluded.name, identity = excluded.identity, "
            "updated_at = excluded.updated_at",
            (device_id, name, identity, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    resp = {
        "success": True,
        "name": name,
        "identity": identity,
        "identity_label": _MEMBER_IDENTITIES.get(identity, ""),
    }
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/admin/members", methods=["GET"])
def api_admin_members():
    """成员署名排行：按姓名聚合本月有效条数（去重加权）与累计成功。

    ⚠️ 放在本文件而非 routes/admin.py：复用上面的月度口径常量与去重键函数，
    避免为一条只读聚合路由跨模块拷贝口径（口径漂移过一次，别再来）。
    未署名的设备聚合进「未署名」，产量不被漏看。鉴权走 /api/admin/* 钩子。
    """
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    _ensure_members_table()
    month_start = _month_start()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT h.device_id, h.original_url, h.canonical_url, h.platform, "
            "h.status, h.created_at, m.name AS member_name, m.identity AS member_identity "
            "FROM history h LEFT JOIN members m ON m.device_id = h.device_id"
        ).fetchall()
    finally:
        conn.close()

    groups = {}
    for row in rows:
        name = str(row["member_name"] or "").strip() or "未署名"
        g = groups.get(name)
        if g is None:
            g = groups[name] = {
                "name": name,
                "identity": "",
                "device_ids": set(),
                "ok_total": 0,
                "month_keys": set(),
                "month_counts": {"douyin": 0, "xiaohongshu": 0, "sph": 0},
                "last_active": "",
            }
        # 同一姓名跨设备时取任一非空身份（同一人不同设备通常选同一种）
        if not g["identity"] and row["member_identity"]:
            g["identity"] = str(row["member_identity"])
        g["device_ids"].add(row["device_id"])
        created = str(row["created_at"] or "")
        if created > g["last_active"]:
            g["last_active"] = created
        if row["status"] != "success":
            continue
        g["ok_total"] += 1
        if created >= month_start:
            key = _month_canonical_key(row["canonical_url"], row["original_url"])
            if key and key not in g["month_keys"]:
                g["month_keys"].add(key)
                platform_key = _MONTHLY_KEYS.get(row["platform"] or "")
                if platform_key:
                    g["month_counts"][platform_key] += 1

    members = []
    for g in groups.values():
        weighted = (g["month_counts"]["douyin"] * _MONTHLY_WEIGHTS["抖音"]
                    + g["month_counts"]["xiaohongshu"] * _MONTHLY_WEIGHTS["小红书"]
                    + g["month_counts"]["sph"] * _MONTHLY_WEIGHTS["视频号"])
        members.append({
            "name": g["name"],
            "identity": g["identity"],
            "identity_label": _MEMBER_IDENTITIES.get(g["identity"], ""),
            "valid_month": int(weighted) if weighted == int(weighted) else round(weighted, 2),
            "month_counts": g["month_counts"],
            "ok_total": g["ok_total"],
            "device_count": len(g["device_ids"]),
            "last_active": g["last_active"],
        })
    members.sort(key=lambda m: (-m["valid_month"], -m["ok_total"], m["name"]))
    # 身份分布（按人数）：让后台一眼看到三种身份各有多少人
    identity_dist = {label: 0 for label in _MEMBER_IDENTITIES.values()}
    identity_dist["未选择"] = 0
    for m in members:
        identity_dist[m["identity_label"] or "未选择"] += 1
    return jsonify({
        "success": True,
        "month": datetime.now().strftime("%Y-%m"),
        "identities": [{"key": k, "label": v} for k, v in _MEMBER_IDENTITIES.items()],
        "identity_dist": identity_dist,
        "members": members,
    })



@app.route("/api/history", methods=["GET"])
def api_history():
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 200", (device_id,)
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


# ------------------------------------------------ 本月有效条数（v1.13.1 · 2026-10-01）

# 计数权重（2026-10-01 口径）：抖音 1 条，小红书 / 视频号各 0.5 条。
# 「有效」= 该设备本月成功提取的记录，按规范链接去重（同一笔记/作品无论
# 用哪种分享形式粘贴，本月只计 1 次）；文案待补（partial）也算有效。
_MONTHLY_WEIGHTS = {"抖音": 1.0, "小红书": 0.5, "视频号": 0.5}
_MONTHLY_KEYS = {"抖音": "douyin", "小红书": "xiaohongshu", "视频号": "sph"}


def _month_start() -> str:
    return datetime.now().strftime("%Y-%m-01 00:00:00")


def _month_canonical_key(canonical_url: str, original_url: str) -> str:
    """去重键：规范链接去掉 query 串；规范链接缺失时退回原始链接。"""
    return str(canonical_url or original_url or "").split("?", 1)[0]


def _load_month_canonical_keys(device_id: str) -> set:
    """本月该设备成功记录的规范链接集合（去 query 串）。"""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT canonical_url, original_url FROM history "
            "WHERE device_id = ? AND status = 'success' AND created_at >= ?",
            (device_id, _month_start()),
        ).fetchall()
    finally:
        conn.close()
    keys = set()
    for row in rows:
        key = _month_canonical_key(row["canonical_url"], row["original_url"])
        if key:
            keys.add(key)
    return keys


@app.route("/api/monthly-summary", methods=["GET"])
def api_monthly_summary():
    """本月有效条数（按设备隔离）：去重后加权，抖音 1 / 小红书 0.5 / 视频号 0.5。"""
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT canonical_url, original_url, platform FROM history "
            "WHERE device_id = ? AND status = 'success' AND created_at >= ?",
            (device_id, _month_start()),
        ).fetchall()
    finally:
        conn.close()

    seen = set()
    counts = {"douyin": 0, "xiaohongshu": 0, "sph": 0}
    for row in rows:
        key = _month_canonical_key(row["canonical_url"], row["original_url"])
        if not key or key in seen:
            continue
        seen.add(key)
        platform_key = _MONTHLY_KEYS.get(row["platform"] or "")
        if platform_key:
            counts[platform_key] += 1

    weighted = sum(
        counts[key] * weight for key, weight in (
            (_MONTHLY_KEYS[label], _MONTHLY_WEIGHTS[label]) for label in _MONTHLY_KEYS
        )
    )
    valid_count = int(weighted) if weighted == int(weighted) else round(weighted, 2)
    resp = {
        "success": True,
        "month": datetime.now().strftime("%Y-%m"),
        "counts": counts,
        "unique_count": len(seen),
        "valid_count": valid_count,
    }
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)
