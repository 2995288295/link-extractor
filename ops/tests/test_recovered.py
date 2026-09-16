"""桩测试：collect_stats 的「已自动补回」口径（真实 SQLite，临时库）。

验证：
  1. 同一条链接「失败 + N 秒后成功」→ 失败不计入有效样本，成功率不受影响；
  2. 失败后**没有**成功（真正没救回来）→ 仍计入失败，成功率下降；
  3. 超出 grace 窗口的补回 → 不算补回；
  4. grace=0 → 关闭该口径（退回旧行为）。

跑法：
  /Users/insta360/.workbuddy/binaries/python/envs/default/bin/python \
      tmp/alert-tuning-20260916/test_recovered.py
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

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


DDL = """
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_url TEXT, canonical_url TEXT, platform TEXT, title TEXT, caption TEXT,
    status TEXT, error TEXT, outcome_class TEXT DEFAULT 'success', error_kind TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0, created_at TEXT
)
"""


def make_db(rows):
    """rows: [(url, outcome_class, error_kind, created_at_text)]"""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.execute(DDL)
    conn.executemany(
        "INSERT INTO history (original_url, platform, outcome_class, error_kind, error, duration_ms, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        [(u, "小红书", oc, ek, ("小红书平台暂时限制访问" if oc != "success" else ""), 1000, ts)
         for (u, oc, ek, ts) in rows],
    )
    conn.commit()
    conn.close()
    return path


def t(offset_seconds):
    return (datetime.now() + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


print("== 1. 失败后 6 秒同链接成功 → 视为已补回 ==")
db = make_db([
    ("u1", "upstream_error", "platform_limited", t(-300)),
    ("u1", "success", "success", t(-294)),
    ("u2", "success", "success", t(-280)),
    ("u3", "success", "success", t(-270)),
])
s = al.collect_stats(db, 15, 180)
check("有效样本=3（补回那条不进分母）", s["valid_total"], 3)
check("服务失败=0", s["service_failures"], 0)
check("已补回=1", s["recovered"], 1)
check("平台限制计数=0", s["limited_total"], 0)
check("成功率=100%", s["service_success_rate"], 100.0)
check("平台维度也干净", s["platforms"]["xiaohongshu"]["service_success_rate"], 100.0)
os.remove(db)

print("== 2. 失败后没有成功 → 仍是真实失败 ==")
db = make_db([
    ("u1", "upstream_error", "platform_limited", t(-300)),
    ("u2", "success", "success", t(-280)),
])
s = al.collect_stats(db, 15, 180)
check("有效样本=2", s["valid_total"], 2)
check("服务失败=1", s["service_failures"], 1)
check("已补回=0", s["recovered"], 0)
check("成功率=50%", s["service_success_rate"], 50.0)
check("平台限制计数=1", s["limited_total"], 1)
os.remove(db)

print("== 3. 补回太晚（超过 grace 180s）→ 不算补回 ==")
db = make_db([
    ("u1", "upstream_error", "platform_limited", t(-600)),
    ("u1", "success", "success", t(-600 + 200)),  # 200s 后 > 180s
])
s = al.collect_stats(db, 15, 180)
check("服务失败仍计入", s["service_failures"], 1)
check("已补回=0", s["recovered"], 0)
os.remove(db)

print("== 4. grace=0 → 关闭口径，退回旧行为 ==")
db = make_db([
    ("u1", "upstream_error", "platform_limited", t(-300)),
    ("u1", "success", "success", t(-294)),
    ("u2", "success", "success", t(-280)),
])
s = al.collect_stats(db, 15, 0)
check("失败照计（valid=3）", s["valid_total"], 3)
check("成功率=66.7%", s["service_success_rate"], 66.7)
check("已补回=0", s["recovered"], 0)
os.remove(db)

print("== 5. 非限流类失败（如页面结构变化）不享受补回豁免 ==")
db = make_db([
    ("u1", "upstream_error", "page_changed", t(-300)),
    ("u1", "success", "success", t(-294)),
])
s = al.collect_stats(db, 15, 180)
check("page_changed 仍算失败", s["service_failures"], 1)
check("已补回=0", s["recovered"], 0)
os.remove(db)

print("\n结果：{} / {} 通过".format(sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
