"""桩测试：验证「持续限流升级档」的触发条件（不联网、不碰真实代理池）。

跑法：
  /Users/insta360/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
      tmp/alert-tuning-20260916/test_escalate.py
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

REAL_START = ex._start_failover_window

# ── 桩：拦住真正开窗口的动作 ──────────────────────────────────────
CALLS = []
ex._start_failover_window = lambda platform, rotate=False, window=None: (
    CALLS.append({"platform": platform, "rotate": rotate, "window": window}) or True
)
ex._current_proxy = lambda: ""

CLOCK = [1000.0]
ex.time.monotonic = lambda: CLOCK[0]


def reset(t):
    CLOCK[0] = t
    CALLS.clear()
    ex._risk_events.clear()
    ex._risk_events_long.clear()


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
check("握住时长 300s", ex.AJIASU_ESCALATE_HOLD, 300.0)
check("短档窗口 60s 未被改动", ex.AJIASU_WINDOW_SECONDS, 60)

print("== 触发条件 ==")
reset(1000.0)
ex._note_risk_failure("xiaohongshu")
ex._note_risk_failure("xiaohongshu")
check("A 短档：90s 内 2 次 → 开默认短窗口", CALLS,
      [{"platform": "xiaohongshu", "rotate": False, "window": None}])

reset(2000.0)
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 2120.0
ex._note_risk_failure("xiaohongshu")
CLOCK[0] = 2240.0
ex._note_risk_failure("xiaohongshu")
check("B 升级档：稀疏 3 次 / 300s 内 → rotate + 300s 长窗口", CALLS,
      [{"platform": "xiaohongshu", "rotate": True, "window": 300.0}])
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
check("D 连击 3 次 → 短档 1 次 + 升级档 1 次（升级用长窗口）", CALLS,
      [{"platform": "douyin", "rotate": False, "window": None},
       {"platform": "douyin", "rotate": True, "window": 300.0}])

print("== 真实 _start_failover_window 是否吃 window 参数 ==")
POOL_ARGS = []
ex._pool_call = lambda args, timeout=None: (
    POOL_ARGS.append(args) or {"ok": True, "proxy": "http://127.0.0.1:1080",
                               "leaseId": "L1", "account": "main", "exitIp": "1.2.3.4"}
)
ex._failover_switch_on = lambda: True
ex._in_freeze_window = lambda: False
CLOCK[0] = 6000.0
ex._failover_history.clear()
started = REAL_START("xiaohongshu", rotate=True, window=300.0)
ttl_ok = any(a[:3] == ["acquire", "--json", "--rotate"] and "315" in a for a in POOL_ARGS)
check("开了窗口", started, True)
check("acquire 的 --ttl = 窗口+15 = 315", ttl_ok, True)
check("窗口到期时间 ≈ now+300", round(ex._failover_state["active_until"] - 6000.0), 300)
with contextlib.suppress(Exception):
    ex._end_failover_window()  # 清掉 timer，避免测试进程挂住

print("\n结果：{} / {} 通过".format(sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
