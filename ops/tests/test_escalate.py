"""桩测试：验证「持续限流升级档」的触发条件（不联网、不碰真实代理池）。

v1.7.0 起升级档的**动作**从「拉长窗口」改成「挂意图 + rotate」，本文件同步更新。

跑法（服务器）：
  PY=/opt/link-extractor/venv/bin/python3 ops/tests/run_all.sh
"""
import contextlib
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 仓库里 extractor.py 在 lib/ 下，测试在 ops/tests/ 下；也允许与测试同目录（本地快照布局）
_CANDIDATES = (HERE, HERE.parent, HERE.parent.parent / "lib", HERE.parent / "lib", HERE / "lib")
_TARGET = next((b / "extractor.py" for b in _CANDIDATES if (b / "extractor.py").is_file()), None)
if _TARGET is None:
    raise SystemExit("找不到 extractor.py（找过：%s）" % ", ".join(str(b) for b in _CANDIDATES))
spec = importlib.util.spec_from_file_location("ex", _TARGET)
ex = importlib.util.module_from_spec(spec)
# 必须先登记进 sys.modules：模块里的 @dataclass 会用 cls.__module__ 反查本模块，
# 查不到就会在 exec_module 阶段炸 AttributeError（不是被改坏了）。
sys.modules["ex"] = ex
spec.loader.exec_module(ex)

REAL_ARM = ex._arm_failover_intent
REAL_CURRENT_PROXY = ex._current_proxy

# ── 桩：拦住真正挂意图 / 取代理的动作 ─────────────────────────────
CALLS = []
ex._arm_failover_intent = lambda platform, rotate=False: (
    CALLS.append({"platform": platform, "rotate": rotate}) or True
)
ex._current_proxy = lambda: ""

CLOCK = [1000.0]
ex.time.monotonic = lambda: CLOCK[0]


def reset(t):
    CLOCK[0] = t
    CALLS.clear()
    ex._risk_events.clear()
    ex._risk_events_long.clear()
    ex._failover_history.clear()


results = []


def check(name, got, want):
    ok = got == want
    print(("  ✅ " if ok else "  ❌ ") + name)
    if not ok:
        print("     got :", got)
        print("     want:", want)
    results.append(ok)


print("== 默认档位 ==")
check("长窗口 300s", ex.AJIASU_ESCALATE_RISK_WINDOW, 300.0)
check("升级阈值 3 次", ex.AJIASU_ESCALATE_THRESHOLD, 3)
check("握代理时长 60s", ex.AJIASU_HOLD_SECONDS, 60.0)
check("意图 TTL 300s", ex.AJIASU_INTENT_TTL, 300.0)
check("等他人取代理 5s", ex.AJIASU_ACQUIRE_WAIT, 5.0)
check("已无 AJIASU_ESCALATE_HOLD（升级档不再拉长窗口）", hasattr(ex, "AJIASU_ESCALATE_HOLD"), False)
check("已无 AJIASU_WINDOW_SECONDS（窗口概念被 hold 取代）", hasattr(ex, "AJIASU_WINDOW_SECONDS"), False)

print("== 触发条件 ==")
reset(1000.0)
ex._note_risk_failure("xiaohongshu")
ex._note_risk_failure("xiaohongshu")
check("A 短档：90s 内 2 次 → 挂意图（不 rotate）", CALLS,
      [{"platform": "xiaohongshu", "rotate": False}])

reset(2000.0)
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 2120.0
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 2240.0
ex._note_risk_failure("xiaohongshu")
check("B 升级档：稀疏 3 次 / 300s 内 → rotate", CALLS,
      [{"platform": "xiaohongshu", "rotate": True}])
check("B 升级后长档计数清零", ex._risk_events_long, [])

reset(3000.0)
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 3400.0
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 3800.0
ex._note_risk_failure("xiaohongshu")
check("C 超窗口淘汰：间隔 400s 不累计 → 不触发", CALLS, [])

reset(5000.0)
for _ in range(3):
    ex._note_risk_failure("douyin")
check("D 连击 3 次 → 短档 1 次 + 升级档 1 次", CALLS,
      [{"platform": "douyin", "rotate": False},
       {"platform": "douyin", "rotate": True}])

# ── 真实实现：核心是「挂意图绝不碰池子」 ──────────────────────────
print("== 真实 _arm_failover_intent：挂意图零消耗 ==")
POOL_ARGS = []
ex._pool_call = lambda args, timeout=None: (
    POOL_ARGS.append(args) or {"ok": True, "proxy": "http://127.0.0.1:1080",
                               "leaseId": "L1", "account": "main", "exitIp": "1.2.3.4"}
)
ex._failover_switch_on = lambda: True
ex._in_freeze_window = lambda: False
ex._current_proxy = REAL_CURRENT_PROXY  # 还原：下面要走真实实现，桩会把它读成空串
CLOCK[0] = 6000.0
ex._failover_history.clear()
ex._failover_state.update({
    "proxy": "", "active_until": 0.0, "timer": None,
    "intent_platform": "", "intent_until": 0.0, "acquiring": False,
})
armed = REAL_ARM("xiaohongshu")
check("挂上意图", armed, True)
check("挂意图期间池子调用 0 次（这是空转被消掉的根因）", POOL_ARGS, [])
check("意图到期时间 ≈ now+300", round(ex._failover_state["intent_until"] - 6000.0), 300)
check("历史已记账（用于每小时上限）", len(ex._failover_history), 1)

print("== 真实 _ensure_proxy：有意图才 acquire ==")
proxy = ex._ensure_proxy()
check("_ensure_proxy 返回代理", proxy, "http://127.0.0.1:1080")
check("acquire 参数 = hold+15 = 75", any(
    a[:3] == ["acquire", "--json", "--rotate"] and "75" in a for a in POOL_ARGS), True)
check("握代理时长 ≈ now+60", round(ex._failover_state["active_until"] - 6000.0), 60)
check("意图已被兑现清空（不会二次 acquire）", ex._failover_state["intent_platform"], "")
check("acquire 次数恰好 1", sum(1 for a in POOL_ARGS if a[:1] == ["acquire"]), 1)
with contextlib.suppress(Exception):
    ex._end_failover_window()  # 清掉 timer，避免测试进程挂住

print("\n结果：{} / {} 通过".format(sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
