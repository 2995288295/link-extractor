import "./styles/index.css";

import { initProfile } from "./features/profile.js";

import "./core/actions.js";

// v1.19.0：内联 onclick/oninput/onkeydown 已全部改为 data-action 事件委托
//（见 core/actions.js），原第一期过渡层（Object.assign(window, {...})）删除。
// 顺带修复：extract.js 的 retryLink 一直在用未 import 的 renderSingleResult
//（重试成功即 ReferenceError），本次随委托改造一并修掉。

// ---------- 移动端交互（C 项） ----------
// 注册 PWA Service Worker（HTTP 环境注册失败会静默忽略，不影响功能）
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/static/sw.js").catch(function () {
      /* HTTP 环境无法注册，忽略 */
    });
  });
}

// 输入框聚焦时自动滚动到可视区，避免手机键盘遮挡
(function () {
  const ta = document.getElementById("inputUrls");
  if (!ta) return;
  const isMobile = () => window.matchMedia("(max-width: 600px)").matches;
  ta.addEventListener("focus", function () {
    if (isMobile()) {
      setTimeout(() => {
        const rect = ta.getBoundingClientRect();
        const viewH = window.innerHeight;
        // 若输入框下半部分超出可视区，滚动到合适位置
        if (rect.bottom > viewH * 0.55) {
          window.scrollTo({ top: ta.offsetTop - 80, behavior: "smooth" });
        }
      }, 300);
    }
  });
})();

// ================= 页面加载 =================
// 口令模块已隐藏（服务端未启用 ACCESS_TOKEN 时不校验），打开即用
window.addEventListener("load", function () {
  // 预留：若以后恢复访问口令，可在此调用 apiFetch("/api/history") 验证
  // 首次访问的引导框（含月度有效条数展示，一次性弹）；已署名则不打扰
  initProfile();
  // 页脚版本号由服务端下发（v1.17.2）：硬编码必然漂移，曾让老大误以为没更新
  fetch("/api/health").then(r => r.json()).then(d => {
    const el = document.getElementById("appVersion");
    if (el && d && d.version) el.textContent = d.version;
  }).catch(() => {});
  // 「读剪贴板提取」按能力显隐（v1.18.1）：clipboard.readText 只在安全上下文
  // （HTTPS/localhost）可用。服务目前是明文 HTTP，按钮**直接隐藏**——
  // 与其放一个点了必定失败的按钮，不如没有；将来上 HTTPS 它会自己出现。
  const clipBtn = document.getElementById("btnClipboard");
  if (clipBtn && !(window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.readText === "function")) {
    clipBtn.classList.add("hidden");
  }
});