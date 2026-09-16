#!/usr/bin/env bash
# 5003 告警 / 换 IP 口径回归测试。
# 全部为桩测试：不联网、不碰真实代理池、不写生产 DB（临时 SQLite 用完即删）。
#
# 用法：ops/tests/run_all.sh          （默认用 5003 的 venv）
#       PY=/usr/bin/python3 ops/tests/run_all.sh
set -u
cd "$(dirname "$0")"
PY="${PY:-/opt/link-extractor/venv/bin/python3}"

[ -x "$PY" ] || { echo "找不到解释器：$PY（可用 PY=... 覆盖）"; exit 2; }

fail=0
for f in test_*.py; do
  printf '%-26s ' "$f"
  if out=$("$PY" "$f" 2>&1); then
    echo "$(echo "$out" | tail -1)"
  else
    echo "❌ FAILED"
    echo "$out" | tail -20
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then echo "全部通过 ✅"; else echo "有失败 ❌"; fi
exit "$fail"
