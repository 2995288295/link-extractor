"""桩测试：告警判定 —— 限流类静音、阈值 80、非限流类照报。

跑法：
  /Users/insta360/.workbuddy/binaries/python/envs/default/bin/python \
      tmp/alert-tuning-20260916/test_alert_logic.py
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 本测试既可能在 ops/tests/ 下跑，也可能与告警脚本同目录 —— 两处都找一下
_TARGET = next((b / "link-extractor-alert.py" for b in (HERE, HERE.parent)
                if (b / "link-extractor-alert.py").is_file()), None)
if _TARGET is None:
    raise SystemExit("找不到 link-extractor-alert.py")
spec = importlib.util.spec_from_file_location("al", _TARGET)
al = importlib.util.module_from_spec(spec)
sys.modules["al"] = al
spec.loader.exec_module(al)

results = []


def check(name, got, want):
    ok = got == want
    print(("  ✅ " if ok else "  ❌ ") + name)
    if not ok:
        print("     got :", got)
        print("     want:", want)
    results.append(ok)


def build_stats(global_rate, platforms, **kw):
    total = kw.pop("total", 10)
    user_errors = kw.pop("user_errors", 0)
    valid = total - user_errors
    st = {
        "available": True, "since": "", "total": total, "ok": int(valid * global_rate / 100),
        "user_errors": user_errors, "valid_total": valid,
        "service_failures": valid - int(valid * global_rate / 100),
        "service_success_rate": global_rate, "platforms": platforms,
        "p95_ms": 0, "limited_total": kw.pop("limited_total", 0),
    }
    st.update(kw)
    return st


def plat(valid, ok, limited=0, limited_short=0):
    return {
        "total": valid, "ok": ok, "valid_total": valid, "limited": limited,
        "limited_short": limited_short,
        "service_success_rate": round(ok / valid * 100, 1) if valid else 100.0,
    }


def run(mode, stats, rate="80"):
    cfg = dict(al.DEFAULTS)
    cfg["ALERT_PLATFORM_ALERTS"] = mode
    cfg["ALERT_SUCCESS_RATE_THRESHOLD"] = rate
    return set(al.evaluate(cfg, True, "", {}, stats))


print("== 阈值 80 ==")
check("85% 不告警（旧阈值 90 会告警）", run("auto", build_stats(85, {})), set())
check("75% 告警", run("auto", build_stats(75, {})), {"service_success_rate"})
check("80% 不算跌破（严格小于）", run("auto", build_stats(80, {})), set())

print("== auto：限流类静音、非限流类照报 ==")
# 小红书全是限流 → （a）不再推、（b）也因限流主导而静音，只留全局那条
lim = build_stats(75, {"xiaohongshu": plat(10, 0, limited=10, limited_short=8)}, limited_total=10)
check("限流主导 → 平台告警全静音，只留全局成功率", run("auto", lim), {"service_success_rate"})

# 小红书没限流但成功率崩（如选择器失效）→ 必须照报
sel = build_stats(90, {"xiaohongshu": plat(10, 2, limited=0)})
check("非限流垮塌 → 报 platform_rate_xiaohongshu", run("auto", sel), {"platform_rate_xiaohongshu"})

# 限流次数超标但占比不高 → （a）静音、（b）不满足（rate 尚可）
minor = build_stats(92, {"xiaohongshu": plat(20, 19, limited=3, limited_short=2)}, limited_total=3)
check("限流 3 次但成功率尚可 → 静音", run("auto", minor), set())

print("== on / off ==")
check("on → 限流类照报 platform_limit_xiaohongshu",
      run("on", build_stats(75, {"xiaohongshu": plat(10, 0, limited=10, limited_short=8)}, limited_total=10)),
      {"service_success_rate", "platform_limit_xiaohongshu"})
check("off → 平台块整块静音",
      run("off", build_stats(75, {"xiaohongshu": plat(10, 0, limited=0)})), {"service_success_rate"})

print("== 默认值 ==")
check("代码默认阈值已改 80", al.DEFAULTS["ALERT_SUCCESS_RATE_THRESHOLD"], "80")
check("默认平台告警模式 auto", al.DEFAULTS["ALERT_PLATFORM_ALERTS"], "auto")
check("样本不足不判定（valid=3）", run("auto", build_stats(50, {}, total=3)), set())

print("\n结果：{} / {} 通过".format(sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
