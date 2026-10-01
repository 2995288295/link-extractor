import "./styles/index.css";

import { switchTab, handleTabKey } from "./ui/tabs.js";
import { openCoverPreview, closeCoverPreview, closeCoverPreviewOnBackdrop } from "./ui/lightbox.js";
import { clearInput, onInputChange } from "./features/input.js";
import { doExtract, retryLink, retryFailedLinks, readClipboardExtract } from "./features/extract.js";
import { copyText } from "./features/copy.js";
import { toggleCaption } from "./features/results.js";
import { loadHistory, clearHistory, setHistoryFilter, onHistorySearch } from "./features/history.js";
import { initProfile, openLoginModal, submitSession, skipMemberName, selectIdentity, logout } from "./features/profile.js";
import { unescapeHtml } from "./core/dom.js";

/* ------------------------------------------------------------------
 * 第一期过渡层：24 处内联 onclick/oninput 依赖全局函数名（原样保留，零改动）。
 * 第三期改为 data-action + 事件委托后，这段连同各模板里的内联属性一起删除。
 *
 * ★ unescapeHtml 必须在这里 —— 第 828 行的内联属性是
 *   onclick="copyText(unescapeHtml(...))"，它在点击时直接取全局 unescapeHtml，
 *   不挂载会 ReferenceError（2.0 方案 §5.7 的清单漏了这一项）。
 * ------------------------------------------------------------------ */
Object.assign(window, {
  switchTab, handleTabKey, clearInput, onInputChange, doExtract, retryFailedLinks,
  copyText, toggleCaption, retryLink, loadHistory, clearHistory,
  openCoverPreview, closeCoverPreview, closeCoverPreviewOnBackdrop,
  unescapeHtml,
  readClipboardExtract, setHistoryFilter, onHistorySearch,
  openLoginModal, submitSession, skipMemberName, selectIdentity, logout,
});

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
});