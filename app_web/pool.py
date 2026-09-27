"""爱加速代理池的 CLI 封装（P5 拆包自 app.py）。

纯子进程调用 + JSON 解析 + 日志读取，**不含任何 HTTP 层**（路由在 `routes/pool.py`）。
拆开的理由：这部分可以脱离 Flask 单独跑 / 单测。

⚠️ 账号密码只走 stdin（`--pass-stdin`），不写日志、不进进程命令行。
⚠️ `_parse_pool_json` 的三级回退不能简化：pool.py 的 `--json` 输出有缩进多行形态，
   只按「单行 JSON」解析会把 status 的整包 payload 丢掉。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from flask import jsonify, request

from .db import _get_db
from .security import _admin_require_rate



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
