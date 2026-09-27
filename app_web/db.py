"""SQLite 长连接与建表（P5 拆包自 app.py）。

`_DbHandle` 是个薄代理：原代码里几乎每个使用点都写 `finally: conn.close()`，
而连接要在线程内复用，所以把 close() 的语义改成「回滚未提交事务」。
**不要**把它换成裸 `sqlite3.Connection`，也别改成 `with closing(...)`。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime


from .config import DB_PATH


# ---------------------------------------------------------------- 数据库

class _DbHandle:
    """线程独占的 SQLite 长连接句柄（薄代理）。

    为什么要代理：本文件几乎所有使用点都写成 `finally: conn.close()`。
    如果 _get_db() 直接返回复用的 Connection，第一次 close 就会把长连接关掉，
    同线程后续拿到的是已关闭连接。这里把 close() 的语义改成「回滚未提交事务」，
    连接本身留在 thread-local 里继续复用；其余属性一律转发底层连接。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def close(self):
        """不真正关闭：只丢弃未提交事务，连接留给本线程复用。"""
        try:
            self._conn.rollback()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)



_db_local = threading.local()



def _new_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL：并发读写不互相阻塞；synchronous=NORMAL 是 WAL 下的官方推荐搭配
    # （只在 checkpoint 时 fsync，写入快很多；掉电最坏丢最近几笔事务，不会损坏库）
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn



def _get_db():
    """取当前线程的 SQLite 连接（首次调用建立，之后复用）。

    ⚠️ 复用只在本进程内成立：gunicorn `-w 1` 单进程 4 线程 → 每线程一条连接。
    sqlite3 连接不可跨线程/跨 fork 使用，若将来改成 `-w >1` 或加 `--preload`，
    这里必须改回「每请求新建 + 真关闭」。
    """
    handle = getattr(_db_local, "handle", None)
    if handle is None:
        handle = _DbHandle(_new_db_conn())
        _db_local.handle = handle
    return handle



def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            original_url TEXT NOT NULL,
            canonical_url TEXT DEFAULT '',
            platform TEXT DEFAULT '',
            title TEXT DEFAULT '',
            caption TEXT DEFAULT '',
            author_name TEXT DEFAULT '',
            publish_time TEXT DEFAULT '',
            like_count INTEGER DEFAULT 0,
            video_url TEXT DEFAULT '',
            cover_url TEXT DEFAULT '',
            status TEXT DEFAULT 'success',
            error TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            cache_hit INTEGER DEFAULT 0,
            outcome_class TEXT DEFAULT 'success',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket_key TEXT PRIMARY KEY,
            timestamps TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_device ON history(device_id, created_at)")
    # 管理看板全局聚合查询加速
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_status ON history(status)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
    if "duration_ms" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN duration_ms INTEGER DEFAULT 0")
    if "cache_hit" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN cache_hit INTEGER DEFAULT 0")
    if "outcome_class" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN outcome_class TEXT DEFAULT 'success'")
    if "error_kind" not in columns:
        # 细粒度归因：由 lib/extractor 的异常类直接给出，不再靠文案猜。
        # 历史行留空 —— 那批数据没有这个信息，无法事后补齐，
        # 聚合查询用「error_kind = '' 时回退文案匹配」来兼容。
        conn.execute("ALTER TABLE history ADD COLUMN error_kind TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_error_kind ON history(error_kind)")
    if "retry_count" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "success_attempt" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN success_attempt INTEGER DEFAULT 0")
    if "retry_reason" not in columns:
        conn.execute("ALTER TABLE history ADD COLUMN retry_reason TEXT DEFAULT ''")
    conn.execute("UPDATE history SET outcome_class = CASE "
                 "WHEN status = 'success' THEN 'success' "
                 "WHEN error LIKE '%不支持%' OR error LIKE '%xsec_token%' OR error LIKE '%作品 ID%' THEN 'invalid_input' "
                 "WHEN error LIKE '%失效%' OR error LIKE '%不存在%' THEN 'expired_content' "
                 "WHEN error LIKE '%风控%' OR error LIKE '%HTML%' OR error LIKE '%JSON%' OR error LIKE '%请求%' THEN 'upstream_error' "
                 "ELSE 'internal_error' END WHERE status != 'success' AND (outcome_class IS NULL OR outcome_class = '' OR outcome_class = 'success')")
    conn.execute("CREATE TABLE IF NOT EXISTS extract_cache (cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_extract_cache_expiry ON extract_cache(expires_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS admin_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL, value REAL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_alerts_created ON admin_alerts(created_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, ip TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at)")
    conn.commit()
    conn.close()



def _admin_audit(action: str, ip: str):
    conn = _get_db()
    try:
        conn.execute("INSERT INTO admin_audit(action, ip, created_at) VALUES (?,?,?)", (action, ip, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
