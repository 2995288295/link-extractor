#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""link-extractor（5003）异常告警：检查服务健康与近窗口成功率，异常时推送飞书群机器人。

设计要点
--------
1. 只看 service_success_rate —— 即剔除 invalid_input / expired_content（用户自己填错或
   链接已失效）之后的口径。否则用户乱贴链接就会把告警刷爆。
2. 「平台暂时限制」（小红书 302 到 /login，XhsAccessDeniedError）是窗口式软限流、会自愈，
   单独归类为 platform_* 告警，与 service_down（进程/端口级）区分开 —— 两者处理方式完全不同：
   前者判断要不要走换 IP 兜底，后者判断要不要重启服务。
3. 同一 kind 的故障 ALERT_REPEAT_MINUTES 分钟内不重复推送；故障消失时补发「已恢复」。
4. 无状态依赖：所有运行状态落在 STATE_FILE，进程可随时重启、可重复执行。
5. 本脚本对 history.db **只读**，不写任何表，避免与 web 进程抢锁。

用法
----
    link-extractor-alert.py              # 正常巡检（供 timer 调用）
    link-extractor-alert.py --dry-run    # 只打印判定结果，不推送、不落状态
    link-extractor-alert.py --test       # 发一条测试卡片，验证 webhook 是否配通
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

CONF_PATH = os.environ.get("ALERT_CONF", "/etc/link-extractor-alert.env")
STATE_FILE = os.environ.get("ALERT_STATE", "/var/lib/link-extractor-alert/state.json")
TOKEN_CACHE = os.environ.get("ALERT_TOKEN_CACHE", "/var/lib/link-extractor-alert/feishu_token.json")

DEFAULTS = {
    # ── 飞书自建应用（推荐：能发私聊，7x24）──────────────────────────────
    "FEISHU_APP_ID": "",
    "FEISHU_APP_SECRET": "",
    # 接收人：用户 open_id（ou_xxx）→ 私聊；或 chat_id（oc_xxx）→ 群。
    # 注意 open_id 是「按应用」隔离的，换应用必须重新获取。
    "FEISHU_RECEIVE_ID": "",
    # ── 飞书群自定义机器人（备选：只能发群）──────────────────────────────
    "FEISHU_WEBHOOK_URL": "",
    "FEISHU_WEBHOOK_SECRET": "",  # 仅在机器人开启「签名校验」时填写
    # 数据源
    "ADMIN_BASE_URL": "http://127.0.0.1:5003",
    "DB_PATH": "/opt/link-extractor/data/history.db",
    # 判定阈值
    "ALERT_WINDOW_MINUTES": "15",  # 成功率统计窗口
    "ALERT_SUCCESS_RATE_THRESHOLD": "90",  # 低于该百分比即告警
    "ALERT_MIN_SAMPLES": "5",  # 窗口内有效样本少于此数则不判定成功率，避免误报
    "ALERT_P95_MS": "5000",  # P95 耗时超过该值预警
    "ALERT_PLATFORM_LIMIT_MIN": "3",  # 单平台「暂时限制」次数达到该值即告警
    "ALERT_HEALTH_FAIL_STREAK": "2",  # 健康检查连续失败几次才算服务挂了
    "ALERT_REPEAT_MINUTES": "30",  # 同一故障静默期
    "ALERT_DASHBOARD_URL": "",  # 可选：后台地址，会附在卡片上
}

TZ_FMT = "%Y-%m-%d %H:%M:%S"


def load_config() -> dict:
    """优先级：配置文件 > 进程环境变量 > 默认值。"""
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in DEFAULTS:
        env_val = os.environ.get(key)
        if env_val:
            cfg[key] = env_val
    return cfg


def cfg_int(cfg: dict, key: str) -> int:
    try:
        return int(float(cfg.get(key) or 0))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def cfg_float(cfg: dict, key: str) -> float:
    try:
        return float(cfg.get(key) or 0)
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("issues", {})
                data.setdefault("health_fail_streak", 0)
                return data
    except (OSError, ValueError):
        pass
    return {"issues": {}, "health_fail_streak": 0}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #

def check_health(base_url: str) -> tuple[bool, str]:
    url = base_url.rstrip("/") + "/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "link-extractor-alert/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, ""
    except urllib.error.HTTPError as exc:
        return False, "HTTP {}".format(exc.code)
    except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
        return False, str(exc)


def collect_stats(db_path: str, window_minutes: int) -> dict:
    """读取窗口内的成功率与平台分布。对 DB 只读。"""
    threshold_ts = (datetime.now() - timedelta(minutes=window_minutes)).strftime(TZ_FMT)
    stats = {
        "available": False,
        "since": threshold_ts,
        "total": 0,
        "ok": 0,
        "user_errors": 0,
        "service_failures": 0,
        "valid_total": 0,
        "service_success_rate": 100.0,
        "platforms": {},
        "p95_ms": 0,
        "limited_total": 0,
    }
    if not os.path.isfile(db_path):
        stats["error"] = "数据库不存在: {}".format(db_path)
        return stats
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True, timeout=5)
    except sqlite3.Error as exc:
        stats["error"] = "打开数据库失败: {}".format(exc)
        return stats

    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome_class = 'success' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN outcome_class IN ('invalid_input','expired_content')
                            THEN 1 ELSE 0 END) AS user_errors,
                   SUM(CASE WHEN outcome_class IN ('upstream_error','internal_error')
                            THEN 1 ELSE 0 END) AS service_failures
            FROM history WHERE created_at >= ?
            """,
            (threshold_ts,),
        ).fetchone()
        total = row["total"] or 0
        ok = row["ok"] or 0
        user_errors = row["user_errors"] or 0
        valid_total = total - user_errors
        stats.update({
            "available": True,
            "total": total,
            "ok": ok,
            "user_errors": user_errors,
            "service_failures": row["service_failures"] or 0,
            "valid_total": valid_total,
            "service_success_rate": round(ok / valid_total * 100, 1) if valid_total else 100.0,
        })

        # 平台维度。
        # 坑：失败记录的 platform 字段经常是空串 —— 请求在短链解析阶段就被平台 302 掉了，
        # 根本走不到「识别平台」那一步。只按 platform 分组会让平台级告警整个失效，
        # 必须回退到 original_url 推断（与 /api/admin/overview 的口径保持一致）。
        for r in conn.execute(
            """
            SELECT CASE
                     WHEN platform IN ('douyin', '抖音') THEN 'douyin'
                     WHEN platform IN ('xiaohongshu', '小红书') THEN 'xiaohongshu'
                     WHEN lower(original_url) LIKE '%douyin%'
                       OR lower(original_url) LIKE '%iesdouyin%' THEN 'douyin'
                     WHEN lower(original_url) LIKE '%xiaohongshu%'
                       OR lower(original_url) LIKE '%xhslink%' THEN 'xiaohongshu'
                     ELSE '' END AS rp,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome_class = 'success' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN outcome_class IN ('invalid_input','expired_content')
                            THEN 1 ELSE 0 END) AS user_errors,
                   SUM(CASE WHEN error LIKE '%平台暂时限制%' THEN 1 ELSE 0 END) AS limited
            FROM history WHERE created_at >= ?
            GROUP BY rp
            """,
            (threshold_ts,),
        ).fetchall():
            if not r["rp"]:
                continue  # 平台无法识别的少量记录，只计入全局口径
            p_valid = (r["total"] or 0) - (r["user_errors"] or 0)
            p_ok = r["ok"] or 0
            stats["platforms"][r["rp"]] = {
                "total": r["total"] or 0,
                "ok": p_ok,
                "valid_total": p_valid,
                "limited": r["limited"] or 0,
                "service_success_rate": round(p_ok / p_valid * 100, 1) if p_valid else 100.0,
            }

        # 全局「平台暂时限制」计数：独立统计，不依赖平台识别是否成功
        stats["limited_total"] = conn.execute(
            "SELECT COUNT(*) AS c FROM history WHERE created_at >= ? AND error LIKE '%平台暂时限制%'",
            (threshold_ts,),
        ).fetchone()["c"] or 0

        # P95 耗时（仅成功样本）
        durations = [
            r["duration_ms"]
            for r in conn.execute(
                "SELECT duration_ms FROM history WHERE created_at >= ? AND duration_ms > 0 ORDER BY duration_ms",
                (threshold_ts,),
            ).fetchall()
        ]
        if durations:
            stats["p95_ms"] = durations[max(0, (len(durations) * 95 + 99) // 100 - 1)]
    finally:
        conn.close()
    return stats


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

PLATFORM_LABEL = {"xiaohongshu": "小红书", "douyin": "抖音"}


def evaluate(cfg: dict, healthy: bool, health_err: str, state: dict, stats: dict) -> dict:
    """返回 {kind: issue}，issue = {severity, title, template, fields, footer}。"""
    issues: dict[str, dict] = {}
    base = cfg.get("ADMIN_BASE_URL", "").rstrip("/")
    dashboard = (cfg.get("ALERT_DASHBOARD_URL") or "").strip()
    win = cfg_int(cfg, "ALERT_WINDOW_MINUTES")
    since_label = "近 {} 分钟".format(win)

    def issue(kind, severity, title, template, fields, footer=""):
        issues[kind] = {
            "severity": severity,
            "title": title,
            "template": template,
            "fields": fields,
            "footer": footer,
        }

    # ---- 1. 服务不可用（最高优先级） ------------------------------------- #
    if not healthy:
        streak = int(state.get("health_fail_streak", 0))
        need = cfg_int(cfg, "ALERT_HEALTH_FAIL_STREAK")
        if streak >= need:
            issue(
                "service_down", "critical",
                "🔴 链接提取 5003 · 服务不可用",
                "red",
                [
                    ("状态", "健康检查连续 {} 次失败".format(streak)),
                    ("错误", (health_err or "无响应")[:120]),
                    ("自愈尝试", "健康检查 timer 会在连续 2 次失败后自动重启"),
                ],
                "服务进程无响应，提取接口当前不可用。{}".format(datetime.now().strftime("%m-%d %H:%M")),
            )
    else:
        state["health_fail_streak"] = 0

    if not stats.get("available"):
        if stats.get("error"):
            issue(
                "stats_unavailable", "warning",
                "🟡 链接提取 5003 · 无法读取统计库",
                "yellow",
                [("错误", stats["error"][:160])],
                "统计口径失效，成功率类告警已跳过。",
            )
        return issues

    samples = stats["valid_total"]
    min_samples = cfg_int(cfg, "ALERT_MIN_SAMPLES")
    enough = samples >= min_samples

    # ---- 2. 全局服务成功率 ----------------------------------------------- #
    rate_threshold = cfg_float(cfg, "ALERT_SUCCESS_RATE_THRESHOLD")
    if enough and stats["service_success_rate"] < rate_threshold:
        # 平台侧限流 vs 服务自身故障，决定「换 IP」还是「查代码」
        if stats["limited_total"] > 0 and stats["limited_total"] >= stats["service_failures"] * 0.6:
            hint = "主要是平台侧暂时限制（软限流），通常会自愈；持续不恢复可考虑换 IP 兜底。"
            template = "orange"
        else:
            hint = "失败以服务侧错误为主，建议看后台失败原因聚合。"
            template = "red"
        fields = [
            ("{}服务成功率".format(since_label), "{}%（阈值 {}%）".format(stats["service_success_rate"], rate_threshold)),
            ("有效样本", "{} 次（已排除 {} 次用户输入问题）".format(samples, stats["user_errors"])),
            ("失败次数", "{} 次，其中平台限制 {} 次".format(stats["service_failures"], stats["limited_total"])),
        ]
        for name, p in sorted(stats["platforms"].items(), key=lambda kv: kv[1]["service_success_rate"]):
            label = PLATFORM_LABEL.get(name, name)
            fields.append(("{}".format(label), "{}%（{} 样本）".format(p["service_success_rate"], p["valid_total"])))
        issue("service_success_rate", "critical", "🟠 链接提取 5003 · 成功率跌破阈值", template, fields, hint)

    # ---- 3. 平台级异常 ---------------------------------------------------- #
    for name, p in stats["platforms"].items():
        label = PLATFORM_LABEL.get(name, name)
        # (a) 平台暂时限制次数超标
        if p["limited"] >= cfg_int(cfg, "ALERT_PLATFORM_LIMIT_MIN"):
            issue(
                "platform_limit_{}".format(name), "warning",
                "🟠 {} 平台暂时限制集中出现".format(label),
                "orange",
                [
                    ("{}".format(since_label), "「平台暂时限制」{} 次".format(p["limited"])),
                    ("该平台成功率", "{}%（{} 样本）".format(p["service_success_rate"], p["valid_total"])),
                    ("含义", "平台把请求 302 到登录页，属窗口式软限流"),
                ],
                "若持续超过 30 分钟不恢复，可考虑启用换 IP 兜底。",
            )
        # (b) 单平台成功率显著偏低（但全局尚可时才有独立价值）
        elif p["valid_total"] >= min_samples and p["service_success_rate"] < rate_threshold:
            issue(
                "platform_rate_{}".format(name), "warning",
                "🟠 {} 成功率偏低".format(label),
                "orange",
                [
                    ("{}".format(since_label), "{}%（{} 样本）".format(p["service_success_rate"], p["valid_total"])),
                    ("全局成功率", "{}%".format(stats["service_success_rate"])),
                ],
                "单平台垮塌、其他平台正常，通常是该平台侧变更或出口 IP 被针对。",
            )

    # ---- 4. P95 耗时 ------------------------------------------------------ #
    p95_limit = cfg_int(cfg, "ALERT_P95_MS")
    if stats["p95_ms"] > p95_limit:
        issue(
            "p95_slow", "warning",
            "🟡 链接提取 5003 · 响应明显变慢",
            "yellow",
            [
                ("{} P95".format(since_label), "{} ms（阈值 {} ms）".format(stats["p95_ms"], p95_limit)),
                ("平均耗时样本", "{} 次".format(stats["total"])),
            ],
            "通常是平台限流或代理链路变慢的前兆。",
        )

    if dashboard:
        for info in issues.values():
            info["footer"] = (info["footer"] + "  看板：{}".format(dashboard)).strip()
    if base and not issues:
        pass
    return issues


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #

def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _http_post_json(url: str, body: dict, headers: dict | None = None) -> tuple[bool, object]:
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return False, "HTTP {} {}".format(exc.code, detail)
    except Exception as exc:  # noqa: BLE001
        return False, "请求异常: {}".format(exc)
    try:
        return True, json.loads(text)
    except ValueError:
        return False, "返回非 JSON: {}".format(text[:200])


def channel_configured(cfg: dict) -> bool:
    app_ok = bool(
        (cfg.get("FEISHU_APP_ID") or "").strip()
        and (cfg.get("FEISHU_APP_SECRET") or "").strip()
        and (cfg.get("FEISHU_RECEIVE_ID") or "").strip()
    )
    return app_ok or bool((cfg.get("FEISHU_WEBHOOK_URL") or "").strip())


def get_tenant_token(cfg: dict) -> tuple[str, str]:
    """取 tenant_access_token。有效期 2 小时，缓存到文件并提前 5 分钟刷新。"""
    app_id = (cfg.get("FEISHU_APP_ID") or "").strip()
    app_secret = (cfg.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        return "", "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"

    try:
        with open(TOKEN_CACHE, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("app_id") == app_id and float(cached.get("expire_at", 0)) > time.time() + 300:
            return cached.get("token", ""), ""
    except (OSError, ValueError):
        pass

    ok, result = _http_post_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if not ok:
        return "", "换 token 失败: {}".format(result)
    if not isinstance(result, dict) or result.get("code") != 0:
        return "", "换 token 被拒: {}".format(str(result)[:200])

    token = result.get("tenant_access_token", "")
    expire = int(result.get("expire", 7200) or 7200)
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        with open(TOKEN_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"app_id": app_id, "token": token, "expire_at": time.time() + expire}, fh)
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass
    return token, ""


def send_via_app(cfg: dict, card: dict) -> tuple[bool, str]:
    """以应用身份发消息：ou_xxx → 私聊，oc_xxx → 群。"""
    token, err = get_tenant_token(cfg)
    if not token:
        return False, err
    receive_id = (cfg.get("FEISHU_RECEIVE_ID") or "").strip()
    id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={}".format(id_type)
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    ok, result = _http_post_json(url, body, {"Authorization": "Bearer {}".format(token)})
    if not ok:
        return False, str(result)
    if isinstance(result, dict) and result.get("code") == 0:
        return True, ""
    return False, "发消息被拒: {}".format(str(result)[:220])


def send_via_webhook(cfg: dict, card: dict) -> tuple[bool, str]:
    webhook = (cfg.get("FEISHU_WEBHOOK_URL") or "").strip()
    if not webhook:
        return False, "未配置 FEISHU_WEBHOOK_URL"
    body = {"msg_type": "interactive", "card": card}
    secret = (cfg.get("FEISHU_WEBHOOK_SECRET") or "").strip()
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = feishu_sign(secret, ts)
    ok, result = _http_post_json(webhook, body)
    if not ok:
        return False, str(result)
    code = result.get("code", result.get("StatusCode", -1)) if isinstance(result, dict) else -1
    if code == 0:
        return True, ""
    return False, "飞书返回: {}".format(str(result)[:200])


def send_message(cfg: dict, card: dict) -> tuple[bool, str]:
    """优先走应用通道（可私聊）；配了 webhook 则作为回退。"""
    app_ok = bool(
        (cfg.get("FEISHU_APP_ID") or "").strip()
        and (cfg.get("FEISHU_APP_SECRET") or "").strip()
        and (cfg.get("FEISHU_RECEIVE_ID") or "").strip()
    )
    webhook_ok = bool((cfg.get("FEISHU_WEBHOOK_URL") or "").strip())

    if app_ok:
        ok, err = send_via_app(cfg, card)
        if ok:
            return True, ""
        if webhook_ok:
            ok2, err2 = send_via_webhook(cfg, card)
            if ok2:
                return True, ""
            return False, "应用通道失败（{}）；回退 webhook 也失败（{}）".format(err, err2)
        return False, err
    if webhook_ok:
        return send_via_webhook(cfg, card)
    return False, "未配置任何飞书通道"


def build_alert_card(issues: dict, win: int) -> dict:
    elements = []
    ordered = sorted(issues.items(), key=lambda kv: (kv[1]["severity"] != "critical", kv[0]))
    for kind, info in ordered:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**{}**".format(info["title"].split("· ")[-1])},
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**{}**\n{}".format(k, v)}}
                for k, v in info["fields"]
            ],
        })
        if info.get("footer"):
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": info["footer"]}],
            })
    worst = "red" if any(i["severity"] == "critical" for i in issues.values()) else "orange"
    n_critical = sum(1 for i in issues.values() if i["severity"] == "critical")
    headline = "🔴 链接提取 5003 · {} 项异常".format(len(issues))
    if n_critical:
        headline = "🔴 链接提取 5003 · {} 项异常（{} 项严重）".format(len(issues), n_critical)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": worst, "title": {"tag": "plain_text", "content": headline}},
        "elements": elements + [{
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "巡检时间 {} · 统计窗口近 {} 分钟".format(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), win)}],
        }],
    }


def build_recovery_card(recovered: list[str], stats: dict, win: int) -> dict:
    lines = []
    for kind in recovered:
        lines.append("✅ {}".format(KIND_LABEL.get(kind, kind)))
    fields = [
        ("恢复项目", "{} 项".format(len(recovered))),
        ("当前成功率", "{}%（{} 样本）".format(stats.get("service_success_rate", "-"), stats.get("valid_total", 0))),
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green", "title": {"tag": "plain_text", "content": "✅ 链接提取 5003 · 已恢复"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**{}**\n{}".format(k, v)}}
                for k, v in fields
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "恢复时间 {} · 统计窗口近 {} 分钟".format(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), win)}]},
        ],
    }


KIND_LABEL = {
    "service_down": "服务不可用（健康检查失败）",
    "service_success_rate": "服务成功率跌破阈值",
    "platform_limit_xiaohongshu": "小红书平台暂时限制",
    "platform_limit_douyin": "抖音平台暂时限制",
    "platform_rate_xiaohongshu": "小红书成功率偏低",
    "platform_rate_douyin": "抖音成功率偏低",
    "p95_slow": "响应耗时偏高（P95）",
    "stats_unavailable": "统计库不可读",
}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def within_silence(last_notified: str, repeat_minutes: int) -> bool:
    try:
        last = datetime.strptime(last_notified, TZ_FMT)
    except (TypeError, ValueError):
        return False
    return datetime.now() - last < timedelta(minutes=repeat_minutes)


def run(dry_run: bool = False) -> int:
    cfg = load_config()
    win = cfg_int(cfg, "ALERT_WINDOW_MINUTES")
    repeat_minutes = cfg_int(cfg, "ALERT_REPEAT_MINUTES")

    # 未配置任何飞书通道时整轮跳过，且不落状态 —— 否则等配置补齐后，
    # 已在发生的故障会被上一条「静默期记录」挡住最长 ALERT_REPEAT_MINUTES 分钟。
    if not dry_run and not channel_configured(cfg):
        print("未配置飞书通道（应用或 webhook），跳过本轮巡检（不落状态）")
        return 0

    state = load_state()
    healthy, health_err = check_health(cfg["ADMIN_BASE_URL"])

    # 健康检查连续失败计数（在 evaluate 前维护）
    if healthy:
        state["health_fail_streak"] = 0
    else:
        state["health_fail_streak"] = int(state.get("health_fail_streak", 0)) + 1

    stats = collect_stats(cfg["DB_PATH"], win)
    issues = evaluate(cfg, healthy, health_err, state, stats)

    current_kinds = set(issues)
    known = state.get("issues", {})

    # 需要新推送的（首次触发 或 已过静默期）
    to_notify = {}
    for kind, info in issues.items():
        prev = known.get(kind) or {}
        if prev.get("active") and within_silence(prev.get("last_notified", ""), repeat_minutes):
            continue  # 静默期内，只更新状态不发消息
        to_notify[kind] = info

    # 已恢复的
    recovered = [k for k, v in known.items() if v.get("active") and k not in current_kinds]

    print("[{ts}] health={h} streak={s} window={w}min total={t} valid={v} rate={r}% limited={l} p95={p}ms issues={i}".format(
        ts=datetime.now().strftime(TZ_FMT), h=healthy, s=state["health_fail_streak"],
        w=win, t=stats.get("total", 0), v=stats.get("valid_total", 0),
        r=stats.get("service_success_rate", "-"), l=stats.get("limited_total", 0),
        p=stats.get("p95_ms", 0), i=sorted(current_kinds) or "无",
    ))

    if dry_run:
        for kind, info in to_notify.items():
            print("  [待推送] {} <- {}".format(kind, info["title"]))
        for kind in recovered:
            print("  [待恢复] {}".format(kind))
        return 0

    sent_any = False
    if to_notify:
        ok, err = send_message(cfg, build_alert_card(to_notify, win))
        sent_any = sent_any or ok
        print("  推送告警 {} 项: {}".format(len(to_notify), "成功" if ok else "失败 -> " + err))
    if recovered:
        ok, err = send_message(cfg, build_recovery_card(recovered, stats, win))
        sent_any = sent_any or ok
        print("  推送恢复 {} 项: {}".format(len(recovered), "成功" if ok else "失败 -> " + err))

    # 落状态
    new_known = dict(known)
    for kind in recovered:
        prev = dict(new_known.get(kind) or {})
        prev.update({"active": False, "last_recovered": datetime.now().strftime(TZ_FMT)})
        new_known[kind] = prev
    for kind in to_notify:
        prev = dict(new_known.get(kind) or {})
        prev.update({"active": True, "last_notified": datetime.now().strftime(TZ_FMT)})
        new_known[kind] = prev
    for kind in current_kinds:  # 静默期内跳过推送的，保持 active 但保留原 last_notified
        new_known.setdefault(kind, {})
    state["issues"] = new_known
    state["last_run"] = datetime.now().strftime(TZ_FMT)
    save_state(state)
    return 0


def channel_label(cfg: dict) -> str:
    app_ok = bool(
        (cfg.get("FEISHU_APP_ID") or "").strip()
        and (cfg.get("FEISHU_APP_SECRET") or "").strip()
        and (cfg.get("FEISHU_RECEIVE_ID") or "").strip()
    )
    if not app_ok:
        return "群机器人 webhook"
    return "飞书应用 · 群" if (cfg.get("FEISHU_RECEIVE_ID") or "").strip().startswith("oc_") else "飞书应用 · 私聊"


def send_test(cfg: dict) -> int:
    stats = collect_stats(cfg["DB_PATH"], cfg_int(cfg, "ALERT_WINDOW_MINUTES"))
    healthy, health_err = check_health(cfg["ADMIN_BASE_URL"])
    channel = channel_label(cfg)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "🔔 链接提取 5003 · 告警通道测试"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**通道已打通。** 后续 5003 出现服务不可用、成功率跌破阈值、平台级异常或响应变慢时，会自动推送到这里。"}},
            {"tag": "hr"},
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**推送通道**\n{}".format(channel)}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**服务状态**\n{}".format("正常" if healthy else "异常：" + health_err[:60])}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**近 {} 分钟成功率**\n{}%（{} 样本）".format(
                    cfg_int(cfg, "ALERT_WINDOW_MINUTES"), stats.get("service_success_rate", "-"), stats.get("valid_total", 0))}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**统计窗口**\n近 {} 分钟".format(cfg_int(cfg, "ALERT_WINDOW_MINUTES"))}},
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "测试时间 {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}]},
        ],
    }
    ok, err = send_message(cfg, card)
    if ok:
        print("测试消息已发送（{}），去飞书里看看。".format(channel))
        return 0
    print("发送失败: {}".format(err))
    return 1


def resolve_open_id(cfg: dict, argv: list[str]) -> int:
    """用手机号或邮箱换 open_id（open_id 按应用隔离，换应用必须重查）。"""
    mobile = email = ""
    for i, a in enumerate(argv):
        if a == "--mobile" and i + 1 < len(argv):
            mobile = argv[i + 1]
        elif a == "--email" and i + 1 < len(argv):
            email = argv[i + 1]
    if not mobile and not email:
        print("用法: link-extractor-alert.py --resolve-openid --mobile 138xxxx 或 --email a@b.com")
        return 2

    token, err = get_tenant_token(cfg)
    if not token:
        print("取 token 失败: {}".format(err))
        return 1

    body: dict = {}
    if mobile:
        body["mobiles"] = [mobile]
    if email:
        body["emails"] = [email]
    ok, result = _http_post_json(
        "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id",
        body,
        {"Authorization": "Bearer {}".format(token)},
    )
    if not ok:
        print("查询失败: {}".format(result))
        return 1
    if not isinstance(result, dict) or result.get("code") != 0:
        print("查询被拒（多半是缺 contact:user.id:readonly 权限）: {}".format(str(result)[:300]))
        return 1
    users = (result.get("data") or {}).get("user_list") or []
    found = False
    for u in users:
        oid = u.get("user_id") or ""
        if oid:
            found = True
            print("open_id = {}".format(oid))
    if not found:
        print("没有查到对应账号（确认手机号/邮箱与飞书账号一致，且该用户在本应用可用范围内）")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if "--resolve-openid" in argv or "--resolve_openid" in argv:
        return resolve_open_id(load_config(), argv)
    if "--test" in argv:
        return send_test(load_config())
    return run(dry_run="--dry-run" in argv or "--dry_run" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
