import "./styles/admin.css";

import { adminFetch, exportCsv, logout, showToast } from "./admin/core.js";
import { overlay, content } from "./admin/state.js";
import { loadAll, refreshCore, currentView } from "./admin/load.js";
import {
  loadPool, poolCheckAll, poolDeepCheck, togglePoolEditor,
  poolToggleFailover, poolCreateAccount, saveAccountEdit,
} from "./admin/pool.js";
// 副作用导入：`initLayoutEditor();` 这行调用留在 layout-editor 的**模块体内**，
// 因为它的执行时机与 DOM 顺序耦合（布局必须先于数据渲染）。这里只 import
// 触发其模块体执行，不再显式调用。
import "./admin/layout-editor.js";

/* 重置成员同步码（v1.18.1）：成员忘记码时的唯一出路。
   置空后该姓名回到开放状态，成员下次填姓名即可进入并重设同步码。
   二次确认：重置会影响该成员多设备同步，别手滑。 */
async function resetMemberCode(name) {
  if (!name) return;
  if (!confirm(`确定重置「${name}」的同步码？\n\n重置后该姓名回到未设置状态，任何设备填这个姓名都能进入并重设同步码。`)) return;
  try {
    await adminFetch("/api/admin/member-reset-code", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    showToast(`已重置 ${name} 的同步码`);
    loadAll();
  } catch (e) {
    showToast(e.message || "重置失败");
  }
}

/* ------------------------------------------------------------------
 * P4 过渡层：后台 HTML 里有 12 处内联 onclick 依赖**全局函数名**
 *   logout / loadAll / exportCsv / loadPool / poolCheckAll / poolDeepCheck /
 *   togglePoolEditor / poolToggleFailover / poolCreateAccount / saveAccountEdit
 * v1 是普通 <script>，函数天然在全局作用域；v2 是 ES module，不挂载就会
 * ReferenceError。HTML 一字未改，故在此显式挂回 window。
 * （2.0 方案 §9.2 的清单漏了这一项 —— 与 P1 时漏掉 unescapeHtml 同源。）
 * P3 换成 data-action + 事件委托后，这段连同 HTML 内联属性一起删除。
 * ------------------------------------------------------------------ */
Object.assign(window, {
  logout, loadAll, exportCsv,
  loadPool, poolCheckAll, poolDeepCheck, togglePoolEditor,
  poolToggleFailover, poolCreateAccount, saveAccountEdit,
  resetMemberCode,
});

document.getElementById("rangeSelect").onchange = loadAll;
document.getElementById("trendMetric").onchange = () => loadAll();
const NAV_TARGETS = { overview: "statusStrip", analysis: "errorList", realtime: "recentList", pool: "poolBody" };
document.querySelectorAll(".nav-tab").forEach(tab => tab.addEventListener("click", () => { document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active")); tab.classList.add("active"); const anchor = document.getElementById(NAV_TARGETS[tab.dataset.view] || "statusStrip"); if (anchor) (anchor.closest(".dashboard-module") || anchor.closest(".card")).scrollIntoView({behavior:"smooth", block:"start"}); if (tab.dataset.view === "pool") loadPool(); }));

(async function init() {
  try {
    await adminFetch("/api/admin/overview");
    overlay.classList.add("hidden");
    content.classList.remove("hidden");
    loadAll();
  } catch (e) {
    overlay.classList.remove("hidden");
  }
})();

/* v1.7.8 轮询：原先 30 秒无条件 loadAll（7 请求 + 1 子进程，切后台照打）。
   现改为「页面可见 + 已登录」才刷，且只刷会变的核心指标。 */
let pollTimer = null;
export function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (document.hidden) return;                        // 后台标签页不刷
    if (content.classList.contains("hidden")) return;   // 未登录不刷
    refreshCore();                                      // 只刷核心指标（3 请求）
    if (currentView() === "pool") loadPool();           // 代理池仅在看着时刷
  }, 30000);
}
export function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { stopPolling(); return; }
  startPolling();
  if (!content.classList.contains("hidden")) refreshCore();   // 回到前台立刻补一次
});

/* 页脚版本号由服务端下发（v1.17.2）：与用户页同源，杜绝硬编码漂移 */
document.addEventListener("DOMContentLoaded", () => {
  fetch("/api/health").then(r => r.json()).then(d => {
    const el = document.getElementById("appVersion");
    if (el && d && d.version) el.textContent = d.version;
  }).catch(() => {});
});
startPolling();