"""访问口令、管理员会话、设备签名（P5 拆包自 app.py）。

三层鉴权**彼此隔离**，改动时务必分清楚：
  - `ACCESS_TOKEN`  → /api/* 的请求头口令（X-Access-Token）
  - `ADMIN_TOKEN`   → /api/admin/* 的管理员口令（X-Admin-Token 或 admin_session cookie）
  - `DEVICE_SECRET` → device_id 的 HMAC 签名，防伪造他人设备越权读历史/统计
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid


from .config import ADMIN_SESSION_TTL, ADMIN_TOKEN, DEVICE_SECRET
from .ratelimit import _check_rate_limit


# ---------------------------------------------------------------- 设备 ID（带签名防伪造）

def _sign_device_id(device_id: str) -> str:
    """用服务端密钥对 device_id 生成 HMAC-SHA256 签名。"""
    return hmac.new(DEVICE_SECRET.encode("utf-8"), device_id.encode("utf-8"), hashlib.sha256).hexdigest()



def _verify_device_signature(device_id: str, signature: str) -> bool:
    """校验 device_id 签名，防止伪造他人 device_id 越权访问历史/统计。"""
    if not device_id or not signature:
        return False
    if len(device_id) > 64 or len(signature) > 128:
        return False
    expected = _sign_device_id(device_id)
    return hmac.compare_digest(expected, signature)



def _get_device(req, issue_new: bool = True):
    """解析并校验请求中的 device_id + 签名。

    返回 (device_id, signature, valid)：
    - 签名有效：直接使用，valid=True
    - 无签名/签名无效：签发新设备（原请求视为新设备），valid=False
      （valid=False 表示设备身份不可信，调用方可跳过设备级限速）
    """
    device_id = req.headers.get("X-Device-Id", "")
    signature = req.headers.get("X-Device-Sig", "")
    if device_id and _verify_device_signature(device_id, signature):
        return device_id, signature, True
    if issue_new:
        new_id = str(uuid.uuid4())
        new_sig = _sign_device_id(new_id)
        return new_id, new_sig, False  # 新签发的可正常使用，但原设备身份不可信
    return device_id, signature, False



def _device_cookie_payload(device_id: str, signature: str):
    """响应中附带的设备标识载荷。"""
    return {"device_id": device_id, "device_sig": signature}



# ---------------------------------------------------------------- 后台运营看板（管理员）

def _admin_require_rate(ip: str) -> bool:
    """管理员接口独立限速（每 IP 每分钟 30 次，看板轮询够用）。"""
    return _check_rate_limit(f"admin:{ip}", 30)



def _admin_session_value(timestamp: int) -> str:
    payload = str(timestamp)
    signature = hmac.new(ADMIN_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"



def _admin_session_valid(value: str) -> bool:
    try:
        payload, signature = value.split(".", 1)
        timestamp = int(payload)
    except (AttributeError, ValueError):
        return False
    if timestamp > int(time.time()) or int(time.time()) - timestamp > ADMIN_SESSION_TTL:
        return False
    expected = _admin_session_value(timestamp).split(".", 1)[1]
    return hmac.compare_digest(signature, expected)
