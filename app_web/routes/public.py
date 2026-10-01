"""用户侧 API（P5 拆包自 app.py）。

`/api/extract` 是 NDJSON 流式接口：每条链接完成即 yield 一行，前端逐条渲染。
并发由 `config._extract_queue`（进程级线程池）承载；单批并发由
`_effective_extract_concurrency()` 在「月底月初保守模式」下自动降档。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, wait
from datetime import datetime, timedelta

from flask import Response, jsonify, request
from lib.extractor import extract_link

from .. import app
from ..config import DEVICE_SECRET, SERVICE_STARTED_AT, VERSION, _effective_extract_concurrency, _extract_queue, log
from ..db import _get_db
from ..outcome import _classify_outcome
from ..ratelimit import _check_rate_limit, _rate_limit_check
from ..security import _admin_require_rate, _device_cookie_payload, _get_device, _sign_device_id

# 每台设备的历史保留条数（v1.13.1 起 200 → 1000）：
# 「本月有效条数」按历史表统计，高产用户一个月可超 200 条，保留过短会静默少算。
_HISTORY_RETENTION_PER_DEVICE = 1000



@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"success": True, "status": "ok", "time": datetime.now().isoformat(), "uptime_seconds": round(time.time() - SERVICE_STARTED_AT), "version": VERSION})



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
        # 保留上限 200 → 1000（v1.13.1）：「本月有效条数」按历史表统计，
        # 高产用户一个月可超 200 条，不放宽会静默少算。
        # ⚠️ v1.17.2：只清理**本月之前**的行——曾按 id 无脑截断，单设备
        # 超过 1000 条时会把本月记录一起删掉，导致月度有效条数静默少算
        # （KPI 口径，错数是不能接受的）。现在 = 最新 1000 条 ∪ 本月全部。
        if record_history:
            try:
                c = _get_db()
                cutoff = c.execute(
                    "SELECT id FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (device_id, _HISTORY_RETENTION_PER_DEVICE),
                ).fetchone()
                if cutoff is not None:
                    c.execute(
                        "DELETE FROM history WHERE device_id = ? AND id <= ? AND created_at < ?",
                        (device_id, cutoff["id"], _month_start()),
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



# ------------------------------------------------ 成员账号（v1.14.0 署名 → v1.18.0 登录体系）

# 身份枚举（v1.17.0）：成员自选一种，后台按身份分组看产量。
# 存 code 不存中文标签——标签调整不必刷历史数据。
_MEMBER_IDENTITIES = {
    "signed": "签约创作者",
    "ambassador": "创作大使",
    "school": "学校/区域创作者",
}

# 两张表的分工（v1.18.0 起）：
#   members         device_id → 姓名 的设备登记表（v1.14.0 就有，保留；
#                    用途：记录某台设备归属哪个成员，供后台统计设备数）
#   member_accounts 姓名 → PIN哈希/身份 的账号表（新增，登录的真正依据）
# history.device_id 在成员登录后**就是姓名**——所有历史/统计/去重逻辑
# 因此自动按人隔离；换设备登录同一姓名即合并，下游查询一行都不用改。

_members_table_ready = False
_accounts_table_ready = False


def _new_member_key() -> str:
    """成员键：ASCII 短键（可安全放进 HTTP 头）。

    为什么不能用中文姓名当凭证：WSGI 按 Latin-1 解析请求头，中文必成乱码，
    HMAC 校验永远失败（v1.18.0 首版实测：登录成功、凭证却用不了）。
    所以头里传 key，服务端 key → 姓名 映射。
    """
    return "m" + uuid.uuid4().hex[:10]


def _ensure_members_table() -> None:
    """设备登记表：device_id → 姓名（v1.14.0 引入，v1.18.0 起只做设备归属登记）。"""
    global _members_table_ready
    if _members_table_ready:
        return
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
        _members_table_ready = True
    finally:
        conn.close()


def _ensure_accounts_table() -> None:
    """账号表：姓名即账号（主键），PIN 哈希 + 身份。"""
    global _accounts_table_ready
    if _accounts_table_ready:
        return
    conn = _get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS member_accounts ("
            "name TEXT PRIMARY KEY, member_key TEXT NOT NULL DEFAULT '', "
            "pin_hash TEXT NOT NULL DEFAULT '', "
            "identity TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(member_accounts)")}
        if "member_key" not in columns:
            # 老库（v1.18.0 首版建的，没有 key 列）补列，下面紧接着补数据
            conn.execute("ALTER TABLE member_accounts ADD COLUMN member_key TEXT NOT NULL DEFAULT ''")
        for r in conn.execute("SELECT name FROM member_accounts WHERE member_key = ''").fetchall():
            conn.execute(
                "UPDATE member_accounts SET member_key = ? WHERE name = ?",
                (_new_member_key(), r["name"]),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_key ON member_accounts(member_key)")
        conn.commit()
        _accounts_table_ready = True
    finally:
        conn.close()


def _hash_pin(pin: str) -> str:
    """PIN 哈希：HMAC(DEVICE_SECRET, pin)。

    4 位 PIN 空间只有 1 万，哈希挡不住针对性爆破——它的作用是「库被看一眼
    不会直接拿到所有人的 PIN」；爆破由 /api/session 的按姓名登录限速负责。
    不值得引 bcrypt：内部小工具， proportionate 即可。
    """
    return hmac.new(DEVICE_SECRET.encode("utf-8"), pin.encode("utf-8"), hashlib.sha256).hexdigest()


def _migrate_to_accounts() -> None:
    """一次性迁移（v1.18.0）：旧「设备→姓名」体系 → 「姓名账号」体系。

    做两件事：
      1. 按姓名建账号（同姓名的多台设备合并成一个账号；PIN 留空，等成员
         首次登录时自行设置；身份继承自已填的 identity）；
      2. history.device_id 回填为姓名，让老数据在新体系下继续可见。

    幂等条件 =「还有 history 行的 device_id 命中 members」。迁完后新行的
    device_id 只会是姓名或匿名 UUID，该条件永不成立 → 重复调用是空操作，
    不需要额外的迁移标记表。
    """
    _ensure_members_table()
    _ensure_accounts_table()
    conn = _get_db()
    try:
        pending = conn.execute(
            "SELECT 1 FROM history WHERE device_id IN (SELECT device_id FROM members WHERE name != '') "
            "OR device_id IN (SELECT name FROM member_accounts) LIMIT 1"
        ).fetchone()
        if not pending:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT name, MAX(CASE WHEN identity != '' THEN identity END) AS identity "
            "FROM members WHERE name != '' GROUP BY name"
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO member_accounts (name, member_key, pin_hash, identity, created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "identity = CASE WHEN excluded.identity != '' THEN excluded.identity "
                "ELSE member_accounts.identity END",
                (r["name"], _new_member_key(), r["identity"] or "", now, now),
            )
        conn.execute(
            "UPDATE history SET device_id = (SELECT a.member_key FROM member_accounts a "
            "WHERE a.name = (SELECT m.name FROM members m WHERE m.device_id = history.device_id)) "
            "WHERE device_id IN (SELECT device_id FROM members WHERE name != '') "
            "AND EXISTS (SELECT 1 FROM member_accounts a WHERE a.name = "
            "(SELECT m.name FROM members m WHERE m.device_id = history.device_id))"
        )
        # 3b) 首版误按姓名存的 → key
        conn.execute(
            "UPDATE history SET device_id = (SELECT member_key FROM member_accounts WHERE name = history.device_id) "
            "WHERE device_id IN (SELECT name FROM member_accounts)"
        )
        conn.commit()
        log.warning("成员体系迁移完成：%d 个姓名账号，history 已按 member_key 回填", len(rows))
    finally:
        conn.close()


@app.route("/api/session", methods=["POST"])
def api_session():
    """进入 / 注册（v1.18.1）：姓名 + **同步码（选填）** + 身份。

    同步码不是密码，是「多设备同步」用的归属声明（老大 v1.18.1 定：选填、
    改名同步码，避免做成密码系统）。规则：

      - 当前设备已是该成员的凭证        → 直接进；填了同步码视为**改码**（免旧码，
                                          登录状态本身就是身份证明）
      - 姓名不存在                     → 以该姓名进入；填了码即设为该姓名的码
      - 姓名存在、**未设码**            → 直接进入（该姓名当前对任何人开放）；
                                          填了码即趁机设上
      - 姓名存在、**已设码**            → 必须填对码才能进入（这就是「保护」的开关）
    忘记码 → 找管理员重置（置空后该姓名回到开放状态，成员重设即可）。
    """
    ip = request.remote_addr or "127.0.0.1"
    # 当前设备标识（此时可能还是匿名 UUID）：用于把本设备登记到成员名下
    cur_device, _cur_sig, _cur_valid = _get_device(request)
    if not _check_rate_limit(ip, 30):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    code = str(payload.get("code") or payload.get("pin") or "").strip()  # 兼容旧字段名 pin
    identity = str(payload.get("identity") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "请输入姓名"}), 400
    if len(name) > 20:
        return jsonify({"success": False, "error": "姓名不能超过 20 个字"}), 400
    if code and not re.fullmatch(r"\d{4}", code):
        return jsonify({"success": False, "error": "同步码需为 4 位数字"}), 400
    if identity and identity not in _MEMBER_IDENTITIES:
        return jsonify({"success": False, "error": "身份选择有误，请重新选择"}), 400
    # 试码防护：按姓名限速（每分钟 5 次）。4 位码只有 1 万组合，不限速等于没设防。
    if not _check_rate_limit(f"member-login:{name}", 5):
        return jsonify({"success": False, "error": "尝试次数过多，请 1 分钟后再试"}), 429

    _ensure_accounts_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT member_key, pin_hash, identity FROM member_accounts WHERE name = ?", (name,)
        ).fetchone()
        final_identity = (row["identity"] if row else "") or ""
        new_key = ""  # 新建账号时先拿住 key，签发要用（row 为 None 时读不到）
        # 当前设备凭证是否已指向这个成员（已登录 → 改码免旧码）
        already = conn.execute(
            "SELECT 1 FROM member_accounts WHERE member_key = ? AND name = ?", (cur_device, name)
        ).fetchone()
        if already and row is not None:
            # 已登录且操作自己的账号：只做「改码 / 改身份」
            if code:
                conn.execute(
                    "UPDATE member_accounts SET pin_hash = ?, updated_at = ? WHERE name = ?",
                    (_hash_pin(code), now, name),
                )
            if identity and identity != final_identity:
                conn.execute(
                    "UPDATE member_accounts SET identity = ?, updated_at = ? WHERE name = ?",
                    (identity, now, name),
                )
                final_identity = identity
        elif row is None:
            # 新姓名：直接以该姓名进入；填了码即设为该姓名的码
            new_key = _new_member_key()
            conn.execute(
                "INSERT INTO member_accounts (name, member_key, pin_hash, identity, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (name, new_key, _hash_pin(code) if code else "", identity, now, now),
            )
            final_identity = identity
        elif row["pin_hash"]:
            # 已设码：必须填对（填空 = 没填码，进不去）
            if not code:
                return jsonify({"success": False, "error": "该姓名已设置同步码，请输入同步码"}), 403
            if not hmac.compare_digest(row["pin_hash"], _hash_pin(code)):
                return jsonify({"success": False, "error": "同步码不正确"}), 403
            if identity and identity != final_identity:
                conn.execute(
                    "UPDATE member_accounts SET identity = ?, updated_at = ? WHERE name = ?",
                    (identity, now, name),
                )
                final_identity = identity
        else:
            # 未设码：开放进入；填了码即趁机设上
            if code:
                conn.execute(
                    "UPDATE member_accounts SET pin_hash = ?, updated_at = ? WHERE name = ?",
                    (_hash_pin(code), now, name),
                )
            if identity and identity != final_identity:
                conn.execute(
                    "UPDATE member_accounts SET identity = ?, updated_at = ? WHERE name = ?",
                    (identity, now, name),
                )
                final_identity = identity
        # 设备登记：这台设备以后归属该成员（供后台统计设备数）
        conn.execute(
            "INSERT INTO members (device_id, name, identity, created_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET name = excluded.name, "
            "identity = CASE WHEN excluded.identity != '' THEN excluded.identity ELSE members.identity END, "
            "updated_at = excluded.updated_at",
            (cur_device, name, final_identity, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    # 以 member_key 作为 device_id 签发——ASCII 才能进 HTTP 头（中文姓名会乱码）。
    # 之后所有请求都按这个人隔离，换设备登录同一姓名即合并。
    member_key = (row["member_key"] if row else new_key) or ""
    resp = {
        "success": True,
        "name": name,
        "identity": final_identity,
        "identity_label": _MEMBER_IDENTITIES.get(final_identity, ""),
        "identities": [{"key": k, "label": v} for k, v in _MEMBER_IDENTITIES.items()],
    }
    resp.update(_device_cookie_payload(member_key, _sign_device_id(member_key)))
    return jsonify(resp)


@app.route("/api/session", methods=["DELETE"])
def api_session_delete():
    """退出登录。凭证在客户端，服务端只负责发一份新的匿名设备载荷；
    前端清掉本地 member_* 后保存它即完成切换（历史不再按人可见）。"""
    device_id, device_sig, _valid = _get_device(request)
    resp = {"success": True}
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    """当前身份。name 为空 = 未登录（匿名）。

    hint_name：本设备**曾经**归属哪个成员（迁移前的旧设备）——仅供登录框
    预填，不代表已登录：历史已按姓名回填，旧设备号下什么都看不到。
    """
    ip = request.remote_addr or "127.0.0.1"
    device_id, device_sig, device_valid = _get_device(request)
    if not _rate_limit_check(ip, device_id if device_valid else ""):
        return jsonify({"success": False, "error": "请求过于频繁，请稍后再试"}), 429

    _migrate_to_accounts()
    conn = _get_db()
    try:
        account = conn.execute(
            "SELECT name, identity, pin_hash FROM member_accounts WHERE member_key = ?", (device_id,)
        ).fetchone()
        hint = ""
        if account is None:
            hint_row = conn.execute(
                "SELECT name FROM members WHERE device_id = ?", (device_id,)
            ).fetchone()
            hint = (hint_row["name"] if hint_row else "") or ""
    finally:
        conn.close()
    account_identity = (account["identity"] if account else "") or ""
    resp = {
        "success": True,
        "name": (account["name"] if account else ""),
        "identity": account_identity,
        "identity_label": _MEMBER_IDENTITIES.get(account_identity, ""),
        # 是否已设同步码：未设时前端在统计页给一条轻量提示（v1.18.3）。
        # 不然已登录成员永远不知道可以去补设，保护状态会一直空着。
        "code_set": bool(account and account["pin_hash"]),
        "hint_name": hint,
        "identities": [{"key": k, "label": v} for k, v in _MEMBER_IDENTITIES.items()],
    }
    resp.update(_device_cookie_payload(device_id, device_sig))
    return jsonify(resp)


@app.route("/api/admin/member-reset-code", methods=["POST"])
def api_admin_member_reset_code():
    """重置某成员的同步码为「未设置」（v1.18.1：忘记码的出路）。

    置空后该姓名回到**开放进入**状态：任何设备填该姓名即可进入并重设同步码。
    不提供自助找回——没有手机号/邮箱，任何自助找回都是绕过同步码的后门；
    「找管理员重置」对这个内部工具就是足够好的流程。
    鉴权走 /api/admin/* 钩子；操作写 journalctl 日志（含操作者 IP）。
    ⚠️ SQL 一律 ? 参数化（Mimosa 曾把 633 行的参数化查询误报为注入——已复核：
       该行是 `WHERE name = ?` + `(name,)` 绑定，无任何拼接）。
    """
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "缺少成员姓名"}), 400
    _ensure_accounts_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    try:
        row = conn.execute("SELECT name FROM member_accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            return jsonify({"success": False, "error": "没有这个成员"}), 404
        conn.execute(
            "UPDATE member_accounts SET pin_hash = '', updated_at = ? WHERE name = ?", (now, name)
        )
        conn.commit()
    finally:
        conn.close()
    log.warning("管理员重置同步码: %s from %s", name, ip)
    return jsonify({"success": True, "name": name})


@app.route("/api/admin/members", methods=["GET"])
def api_admin_members():
    """成员署名排行：按姓名聚合本月有效条数（去重加权）与累计成功。

    ⚠️ 放在本文件而非 routes/admin.py：复用上面的月度口径常量与去重键函数，
    避免为一条只读聚合路由跨模块拷贝口径（口径漂移过一次，别再来）。
    v1.18.0 起 history.device_id 在成员登录后即姓名，直接按它分组；
    设备数另查 members 登记表（匿名设备聚合进「未署名」，产量不被漏看）。
    """
    ip = request.remote_addr or "127.0.0.1"
    if not _admin_require_rate(ip):
        return jsonify({"success": False, "error": "请求过于频繁"}), 429

    _migrate_to_accounts()
    month_start = _month_start()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT h.device_id, h.original_url, h.canonical_url, h.platform, "
            "h.status, h.created_at, a.name AS member_name, a.identity AS member_identity "
            "FROM history h LEFT JOIN member_accounts a ON a.member_key = h.device_id"
        ).fetchall()
        device_rows = conn.execute(
            "SELECT name, COUNT(*) AS devices FROM members WHERE name != '' GROUP BY name"
        ).fetchall()
    finally:
        conn.close()
    device_count = {r["name"]: r["devices"] for r in device_rows}

    groups = {}
    for row in rows:
        account_hit = row["member_name"] is not None
        name = str(row["member_name"] or "") if account_hit else "未署名"
        g = groups.get(name)
        if g is None:
            g = groups[name] = {
                "name": name,
                "identity": (str(row["member_identity"]) if account_hit and row["member_identity"] else ""),
                "ok_total": 0,
                "month_keys": set(),
                "month_counts": {"douyin": 0, "xiaohongshu": 0, "sph": 0},
                "last_active": "",
            }
        if account_hit and not g["identity"] and row["member_identity"]:
            g["identity"] = str(row["member_identity"])
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
            "device_count": device_count.get(g["name"], 1 if g["name"] != "未署名" else 0),
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
