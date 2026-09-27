"""进程内滑动窗口限速器（P5 拆包自 app.py）。

⚠️ 单例语义：`_rate_buckets` 是**模块级** dict，靠 gunicorn `-w 1` 单进程 4 线程天然共享，
   加锁即正确。**不要**把它搬进类实例或工厂作用域，也不要在多 worker 下直接复用 ——
   那条路的前置改造见方案 §10.2。

⚠️ `_rate_last_persist` 的重新赋值发生在 `_persist_rate_buckets()` 里，用的是 `global`。
   拆包时刻意把它与定义留在同一个模块，所以那句 `global` 一个字都不用改。
"""

from __future__ import annotations

import json
import os
import threading
import time


from .config import log
from .db import _get_db



# ---------------------------------------------------------------- 速率限制（内存滑动窗口 + 定期落库）

RATE_IP_PER_MINUTE = int(os.environ.get("RATE_IP_PER_MINUTE", "20"))      # 每 IP 每分钟

RATE_DEVICE_PER_MINUTE = int(os.environ.get("RATE_DEVICE_PER_MINUTE", "15"))  # 每设备每分钟

_RATE_WINDOW = 60  # 秒


# 2026-09-27：原来是「查 SQLite → 改 list → 写回」，三步之间没有事务也没有锁，
# 单进程 4 线程会各自读到同一份旧数据再互相覆盖 ——
# 实测并发 40 次 / 限额 20 → 放行 40 次（超额 2 倍）；且每次检查都要抢一次写锁
# （40 次共 545ms，空载单次仅 0.32ms）。
# 现在改为进程内内存滑动窗口（gunicorn `-w 1` 单进程 4 线程天然共享，加锁即正确），
# 再定期把窗口快照落回 rate_limits 表（保留原表结构，重启后计数不重置）。
_rate_lock = threading.Lock()

_rate_buckets = {}          # bucket_key -> 窗口内时间戳列表（升序）

_rate_persist_lock = threading.Lock()

_rate_last_persist = 0.0

_RATE_PERSIST_INTERVAL = 15   # 秒：最多每 15 秒落库一次

_RATE_GC_THRESHOLD = 2000     # 内存桶数超过此值时顺带清理过期桶



def _rate_gc_locked(now: float):
    """清掉窗口外的桶（调用方须持有 _rate_lock）。"""
    cutoff = now - _RATE_WINDOW
    for key in [k for k, v in _rate_buckets.items() if not v or v[-1] <= cutoff]:
        del _rate_buckets[key]



def _check_rate_limit(bucket_key: str, limit: int) -> bool:
    """进程内滑动窗口限速；超限返回 False。"""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    try:
        with _rate_lock:
            timestamps = [t for t in _rate_buckets.get(bucket_key, ()) if t > cutoff]
            if len(timestamps) >= limit:
                _rate_buckets[bucket_key] = timestamps
                return False
            timestamps.append(now)
            _rate_buckets[bucket_key] = timestamps
            if len(_rate_buckets) > _RATE_GC_THRESHOLD:
                _rate_gc_locked(now)
    except Exception:
        # 限速失败时放行（避免限速器本身成为故障点）
        return True
    _persist_rate_buckets()
    return True



def _persist_rate_buckets(force: bool = False):
    """把内存窗口快照落库（节流 + 非阻塞：抢不到锁或不到间隔就跳过）。"""
    global _rate_last_persist
    now = time.time()
    if not force and now - _rate_last_persist < _RATE_PERSIST_INTERVAL:
        return
    if not _rate_persist_lock.acquire(blocking=False):
        return
    try:
        with _rate_lock:
            _rate_last_persist = now
            if not _rate_buckets:
                return
            snapshot = {k: list(v) for k, v in _rate_buckets.items()}
        conn = _get_db()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO rate_limits (bucket_key, timestamps, updated_at) VALUES (?,?,?)",
                [(k, json.dumps(v), now) for k, v in snapshot.items()],
            )
            conn.execute("DELETE FROM rate_limits WHERE updated_at < ?", (now - 3600,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("rate_limits 落库失败（不影响限速本身）", exc_info=True)
    finally:
        _rate_persist_lock.release()



def _load_rate_buckets():
    """启动时把未过期窗口载入内存（服务重启不重置限速计数）。"""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT bucket_key, timestamps FROM rate_limits WHERE updated_at >= ?",
                (now - 3600,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                ts = [t for t in json.loads(row["timestamps"]) if t > cutoff]
            except (ValueError, TypeError):
                continue
            if ts:
                _rate_buckets[row["bucket_key"]] = ts
        if _rate_buckets:
            log.info("限速窗口已载入：%d 个活跃桶", len(_rate_buckets))
    except Exception:
        log.debug("限速窗口载入失败（从零开始计数）", exc_info=True)



def _rate_limit_check(ip: str, device_id: str) -> bool:
    """IP + 设备双维度限速。

    设备维度仅在客户端提供了有效签名时生效（签名无效/未提供时
    _get_device 已签发新设备，但这里用原始值判断，避免无签名请求
    每次换新设备绕过设备限速）。
    """
    if not _check_rate_limit(f"ip:{ip}", RATE_IP_PER_MINUTE):
        return False
    if device_id and not _check_rate_limit(f"dev:{device_id}", RATE_DEVICE_PER_MINUTE):
        return False
    return True
