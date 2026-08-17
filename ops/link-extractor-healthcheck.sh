#!/usr/bin/env bash
# 连续两次本机健康检查失败才重启，避免单次网络抖动导致服务中断。
set -eu

STATE_FILE=/run/link-extractor-healthcheck.failures
HEALTH_URL=http://127.0.0.1:5003/api/health

if /usr/bin/curl --fail --silent --show-error --max-time 15 "$HEALTH_URL" >/dev/null; then
    rm -f "$STATE_FILE"
    exit 0
fi

failures=0
if [ -f "$STATE_FILE" ]; then
    failures=$(cat "$STATE_FILE" 2>/dev/null || printf '0')
fi
failures=$((failures + 1))
printf '%s\n' "$failures" > "$STATE_FILE"
/usr/bin/logger -t link-extractor-healthcheck "health check failed (${failures}/2)"

if [ "$failures" -ge 2 ]; then
    /usr/bin/logger -t link-extractor-healthcheck "restarting link-extractor after consecutive health check failures"
    /usr/bin/systemctl restart link-extractor
    rm -f "$STATE_FILE"
fi
