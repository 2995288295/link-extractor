"""代理池运维 API（P5 拆包自 app.py，仅管理员）。

⚠️ 这是**唯一能真正改生产状态**的界面：换 IP 兜底开关、释放 / 回收租约、账号增删改
   都会真的消耗爱加速额度或改动出口 IP。改这里的代码时验收**只验按钮存在与绑定未断**，
   不要实际点击（方案 §9.3）。
"""

from __future__ import annotations

import re

from flask import jsonify, request

from .. import app
from ..db import _admin_audit
from ..pool import POOL_CMD_TIMEOUT, POOL_STATUS_TIMEOUT, _pool_precheck, _pool_recent_events, _pool_run, _read_pool_config, _risk_trend



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
