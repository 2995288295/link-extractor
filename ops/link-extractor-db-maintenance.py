#!/usr/bin/env python3
"""每日 SQLite 轻量维护：清理过期记录并优化查询统计。"""
from pathlib import Path
import sqlite3
import time

db_path = Path(__file__).resolve().parents[1] / "data" / "history.db"
if not db_path.exists():
    raise SystemExit(0)

now = time.time()
with sqlite3.connect(db_path, timeout=15) as conn:
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("DELETE FROM rate_limits WHERE updated_at < ?", (now - 3600,))
    conn.execute("DELETE FROM extract_cache WHERE expires_at < ?", (now,))
    conn.execute("PRAGMA optimize")
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    except sqlite3.OperationalError:
        # 服务繁忙时跳过检查点；下一次定时维护会再次尝试。
        pass
