import { state, overlay, content } from "./state.js";
import { loadAll } from "./load.js";

export function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 1600);
}

export function readCookie(name) {
  const item = document.cookie.split("; ").find(v => v.startsWith(name + "="));
  return item ? decodeURIComponent(item.slice(name.length + 1)) : "";
}

export async function adminFetch(url) {
  const headers = state.adminToken ? { "X-Admin-Token": state.adminToken } : {};
  const csrf = readCookie("csrf_token");
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const res = await fetch(url, { headers, credentials: "same-origin" });
  if (res.status === 401 || res.status === 503) {
    throw new Error("unauthorized");
  }
  const json = await res.json().catch(() => ({}));
  if (!json.success) throw new Error(json.error || "请求失败");
  return json;
}

export async function login() {
  const input = document.getElementById("adminTokenInput");
  const val = input.value.trim();
  if (!val) return;
  const remember = document.getElementById("adminRemember").checked;
  try {
    const res = await fetch("/api/admin/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify({ token: val, remember: remember }),
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok || !result.success) throw new Error(result.error || "登录失败");
    await adminFetch("/api/admin/overview");
    overlay.classList.add("hidden");
    content.classList.remove("hidden");
    showToast(remember ? "登录成功，90 天内免登录" : "登录成功");
    loadAll();
  } catch (e) {
    showToast(e.message.includes("频繁") ? e.message : "口令错误");
  }
}
document.getElementById("loginBtn").onclick = login;
document.getElementById("adminTokenInput").onkeydown = (e) => { if (e.key === "Enter") login(); };

export async function logout() {
  await fetch("/api/admin/logout", { method: "POST", headers: { "X-CSRF-Token": readCookie("csrf_token") }, credentials: "same-origin" }).catch(() => {});
  state.adminToken = "";
  content.classList.add("hidden");
  overlay.classList.remove("hidden");
  document.getElementById("adminTokenInput").value = "";
  showToast("已退出");
}

export async function exportCsv() {
  const range = document.getElementById("rangeSelect").value;
  const res = await fetch(`/api/admin/export.csv?range=${encodeURIComponent(range)}`, { headers: { "X-CSRF-Token": readCookie("csrf_token") }, credentials: "same-origin" });
  if (!res.ok) { showToast("导出失败"); return; }
  const blob = await res.blob();
  const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "link-extractor-report.csv"; link.click(); URL.revokeObjectURL(link.href);
}

export function fmt(n) { return (n ?? 0).toLocaleString("zh-CN"); }
export function esc(s) { return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }