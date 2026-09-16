"""桩测试：验证「按需开窗」（挂意图 + _ensure_proxy）——不联网、不碰真实代理池。

这是 v1.7.0 的核心回归：**没有请求要用代理时，一秒额度都不该花**。

跑法（服务器）：
  PY=/opt/link-extractor/venv/bin/python3 ops/tests/run_all.sh
"""
import importlib.util
import pathlib
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
_CANDIDATES = (HERE, HERE.parent, HERE.parent.parent / "lib", HERE.parent / "lib", HERE / "lib")
_TARGET = next((b / "extractor.py" for b in _CANDIDATES if (b / "extractor.py").is_file()), None)
if _TARGET is None:
    raise SystemExit("找不到 extractor.py（找过：%s）" % ", ".join(str(b) for b in _CANDIDATES))
spec = importlib.util.spec_from_file_location("ex", _TARGET)
ex = importlib.util.module_from_spec(spec)
sys.modules["ex"] = ex
spec.loader.exec_module(ex)

results = []
POOL = []


def check(name, got, want):
    ok = got == want
    print(("  ✅ " if ok else "  ❌ ") + name)
    if not ok:
        print("     got :", got)
        print("     want:", want)
    results.append(ok)


def fake_pool(args, timeout=None):
    """记录调用；acquire 成功返回一个假代理。"""
    POOL.append(list(args))
    if args[0] == "acquire":
        return {"ok": True, "proxy": "http://127.0.0.1:1080",
                "leaseId": "L1", "account": "main", "exitIp": "1.2.3.4", "node": "vvn-0001-0001"}
    return {"state": "released"}


def install_stubs(pool=fake_pool, switch=True, frozen=False):
    global POOL
    POOL = []
    ex._pool_call = pool
    ex._failover_switch_on = lambda: switch
    ex._in_freeze_window = lambda: frozen


def reset_state(proxy="", active_until=0.0, intent="", intent_until=0.0):
    state = ex._failover_state
    timer = state.get("timer")
    if timer is not None:
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass
    state.update({
        "proxy": proxy, "active_until": active_until, "lease_id": "", "account": "",
        "exit_ip": "", "timer": None,
        "intent_platform": intent, "intent_until": intent_until, "acquiring": False,
    })
    ex._failover_history.clear()
    ex._risk_events.clear()
    ex._risk_events_long.clear()


def acquisitions():
    return sum(1 for a in POOL if a[:1] == ["acquire"])


print("== 1. 没有意图：_ensure_proxy 必须零阻塞、零消耗（空转根治断言） ==")
install_stubs()
reset_state()
check("返回空串（走直连）", ex._ensure_proxy(), "")
check("池子零调用", POOL, [])
check("_intent_active 为 False", ex._intent_active(), False)

print("== 2. 意图已过期：同样不该花钱 ==")
install_stubs()
reset_state(intent="xiaohongshu", intent_until=time.monotonic() - 1)
check("返回空串", ex._ensure_proxy(), "")
check("池子零调用", POOL, [])
check("_intent_active 为 False", ex._intent_active(), False)

print("== 3. 有未过期意图：此刻才 acquire，且只 acquire 一次 ==")
install_stubs()
reset_state(intent="xiaohongshu", intent_until=time.monotonic() + 60)
got = ex._ensure_proxy()
check("拿到代理", got, "http://127.0.0.1:1080")
check("acquire 恰好 1 次", acquisitions(), 1)
check("意图在兑现时被清空（不会二次 acquire）", ex._failover_state["intent_platform"], "")
check("意图到期被清空", ex._failover_state["intent_until"], 0.0)
check("握着时长 ≈ 60s（计时起点 = 有请求时）",
      round(ex._failover_state["active_until"] - time.monotonic()), 60)
check("再次调用直接复用，不再 acquire", ex._ensure_proxy(), "http://127.0.0.1:1080")
check("acquire 仍为 1 次", acquisitions(), 1)

print("== 4. acquire 失败：返回空串、不反复重试（防 acquire 风暴） ==")


def failing_pool(args, timeout=None):
    POOL.append(list(args))
    if args[0] == "acquire":
        return {"ok": False, "state": "exhausted", "message": "额度已用尽"}
    return {"state": "released"}


install_stubs(pool=failing_pool)
reset_state(intent="xiaohongshu", intent_until=time.monotonic() + 60)
check("取不到代理", ex._ensure_proxy(), "")
check("acquire 尝试 1 次", acquisitions(), 1)
check("意图已清空", ex._failover_state["intent_platform"], "")
check("第二次调用不再 acquire（池子仍为 1 次）", (ex._ensure_proxy(), acquisitions()), ("", 1))

print("== 5. rotate（升级档）：先放掉现有租约，逼下一次换线路 ==")
install_stubs()
reset_state(proxy="http://127.0.0.1:1080", active_until=time.monotonic() + 30)
check("先有活动代理", ex._current_proxy(), "http://127.0.0.1:1080")
armed = ex._arm_failover_intent("xiaohongshu", rotate=True)
check("挂上意图", armed, True)
check("release 被调用", any(a[:1] == ["release"] for a in POOL), True)
check("旧代理已释放", ex._current_proxy(), "")

print("== 6. 冻结窗口 / 热开关：连意图都不挂 ==")
install_stubs(frozen=True)
reset_state()
check("冻结窗口内不挂意图", ex._arm_failover_intent("xiaohongshu"), False)
check("意图仍为空", ex._failover_state["intent_platform"], "")
install_stubs(switch=False)
reset_state()
check("热开关关闭时不挂意图", ex._arm_failover_intent("xiaohongshu"), False)

print("== 7. 每小时上限：对「意图」计数 ==")
install_stubs()
reset_state()
now = time.monotonic()
ex._failover_history.extend([now - i for i in range(ex.AJIASU_MAX_PER_HOUR)])
check("已达上限 → 挂不上", ex._arm_failover_intent("xiaohongshu"), False)
check("意图仍为空", ex._failover_state["intent_platform"], "")

print("== 8. 最小间隔：短档受限，rotate 不受限 ==")
install_stubs()
reset_state()
ex._failover_history.append(time.monotonic())
check("短档 30s 内第二次 → 挂不上", ex._arm_failover_intent("xiaohongshu"), False)
check("rotate 可以挂上", ex._arm_failover_intent("xiaohongshu", rotate=True), True)

print("== 9. 并发单飞：4 个线程只 acquire 一次，全部复用同一份代理 ==")
CONC = []


def slow_pool(args, timeout=None):
    CONC.append(list(args))
    if args[0] == "acquire":
        time.sleep(0.4)  # 模拟真实 connect（实测约 3s）
        return {"ok": True, "proxy": "http://127.0.0.1:1080",
                "leaseId": "L9", "account": "main", "exitIp": "9.9.9.9", "node": "vvn-9999-9999"}
    return {"state": "released"}


install_stubs(pool=slow_pool)
reset_state(intent="xiaohongshu", intent_until=time.monotonic() + 60)
barrier = threading.Barrier(4)
got = []
got_lock = threading.Lock()


def worker():
    barrier.wait()
    value = ex._ensure_proxy()
    with got_lock:
        got.append(value)


threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=20)
check("4 个线程都拿到代理", sorted(got), ["http://127.0.0.1:1080"] * 4)
check("只 acquire 了一次（1080 单连接不被互踩）",
      sum(1 for a in CONC if a[:1] == ["acquire"]), 1)
try:
    ex._end_failover_window()
except Exception:  # noqa: BLE001
    pass

print("\n结果：{} / {} 通过".format(sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
