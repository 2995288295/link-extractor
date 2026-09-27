import { state } from "./state.js";

export function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/** 统一的 API 请求封装：自动带设备签名，处理限流 */
export async function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  headers["Content-Type"] = "application/json";
  if (state.deviceId) headers["X-Device-Id"] = state.deviceId;
  if (state.deviceSig) headers["X-Device-Sig"] = state.deviceSig;

  const res = await fetch(url, { ...options, headers });

  if (res.status === 429) {
    const json = await res.json().catch(() => ({}));
    throw new Error(json.error || "请求过于频繁");
  }
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    throw new Error(json.error || "请求失败");
  }
  return handleDevicePayload(res);
}

/** 从响应中提取并保存 device_id + device_sig（后端签发的） */
export async function handleDevicePayload(res) {
  const clone = res.clone();
  const json = await res.json().catch(() => ({}));
  if (json.device_id && json.device_sig) {
    state.deviceId = json.device_id;
    state.deviceSig = json.device_sig;
    localStorage.setItem("device_id", state.deviceId);
    localStorage.setItem("device_sig", state.deviceSig);
  }
  return json;
}

/** 构建带设备签名的请求头 */
export function buildHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (state.deviceId) headers["X-Device-Id"] = state.deviceId;
  if (state.deviceSig) headers["X-Device-Sig"] = state.deviceSig;
  return headers;
}