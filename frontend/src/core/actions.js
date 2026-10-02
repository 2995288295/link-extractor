/* ============================================================================
 * core/actions · data-action 事件委托中枢（v1.19.0）
 *
 * 把内联 onclick/oninput/onkeydown 全部收口到这里（约 40 处）：
 *   - 静态壳 index.html（tab / 工具栏 / 历史筛选 / 登录框 / 弹层）
 *   - results.js / history.js / stats.js 模板字符串里的 data-action="..."
 * 处理器统一签名 (event, el)：event 是原始事件，el 是带 data-action 的元素。
 * 需要「精确命中」语义的动作（弹层背板）自行比对 event.target === el。
 *
 * 不走 data-action 的三处直接绑定（见文件末尾）：输入框 input、
 * 历史搜索 input、登录框两个输入框的 Enter。
 *
 * 注意：admin 页不在本期，仍走 admin.js 的 Object.assign(window) 过渡层。
 * ========================================================================== */
import { switchTab, handleTabKey } from "../ui/tabs.js";
import { openCoverPreview, closeCoverPreview } from "../ui/lightbox.js";
import { clearInput, onInputChange } from "../features/input.js";
import { doExtract, retryLink, retryFailedLinks, readClipboardExtract, copyAllLinks, copyAllCaptions } from "../features/extract.js";
import { copyText } from "../features/copy.js";
import { toggleCaption } from "../features/results.js";
import { loadHistory, clearHistory, setHistoryFilter, onHistorySearch } from "../features/history.js";
import { openLoginModal, submitSession, skipMemberName, selectIdentity, logout } from "../features/profile.js";
import { unescapeHtml } from "./dom.js";

const ACTIONS = {
  /* ── 导航 ─────────────────────────────────────────────── */
  "switch-tab": (event, el) => switchTab(el.dataset.tab),

  /* ── 输入 / 提取 ──────────────────────────────────────── */
  "do-extract": () => doExtract(),
  "read-clipboard": () => readClipboardExtract(),
  "clear-input": () => clearInput(),
  "retry-failed": () => retryFailedLinks(),

  /* ── 结果卡（results.js 模板） ────────────────────────── */
  // 复制链接：原内联写法是穿越三层父节点的 DOM 游走
  //（this.parentNode.previousElementSibling.previousElementSibling.querySelector('.field-value')），
  // 语义 = 结果卡主区里第一个 .field-value.link（转换链接），等价改写为 closest 查找。
  // unescapeHtml 是历史行为，原样保留。
  "copy-canonical": (event, el) => {
    const card = el.closest(".result-item");
    const node = card && card.querySelector(".result-main .field-value.link");
    if (node) copyText(unescapeHtml(node.innerText), el);
  },
  // 复制文案：原内联 = this.parentNode.parentNode.querySelector('.caption')
  //（copy-row → result-content），等价于结果卡主区里的 .caption。
  "copy-caption": (event, el) => {
    const card = el.closest(".result-item");
    const node = card && card.querySelector(".result-main .caption");
    if (node) copyText(node.innerText, el);
  },
  // 复制原链接 / 复制原视频链接：值都在 .result-item 的 data-* 上
  "copy-attr": (event, el) => {
    const card = el.closest(".result-item");
    if (!card) return;
    copyText(card.dataset[el.dataset.copyAttr] || "", el);
  },
  "retry-link": (event, el) => {
    const card = el.closest(".result-item");
    const box = document.getElementById("results");
    // retryLink 用 index 决定重试成功后卡片插回的位置，内联时代传渲染序号，
    // 委托后按当前 DOM 位置现算
    const index = card && box ? [...box.children].indexOf(card) : -1;
    retryLink(el, index);
  },
  "toggle-caption": (event, el) => toggleCaption(el),

  /* ── 封面弹层 ─────────────────────────────────────────── */
  "open-cover-preview": (event, el) => openCoverPreview(el.dataset.src, el.dataset.alt),
  "close-cover-preview": () => closeCoverPreview(),
  // 背板点击只允许精确命中背板本身（点弹层内容不关）
  "close-cover-backdrop": (event, el) => {
    if (event.target === el) closeCoverPreview();
  },

  /* ── 批量复制（v1.19.0 新增） ─────────────────────────── */
  "copy-all-links": (event, el) => copyAllLinks(el),
  "copy-all-captions": (event, el) => copyAllCaptions(el),

  /* ── 历史记录 ─────────────────────────────────────────── */
  "load-history": () => loadHistory(),
  "clear-history": () => clearHistory(),
  "set-history-filter": (event, el) => setHistoryFilter(el.dataset.platform),
  // 历史卡（history.js 模板）：容器是 .history-card-content
  "copy-history-link": (event, el) => {
    const scope = el.closest(".history-card-content");
    const node = scope && scope.querySelector(".link");
    if (node) copyText(node.innerText, el);
  },
  "copy-history-caption": (event, el) => {
    const scope = el.closest(".history-card-content");
    const node = scope && scope.querySelector(".caption");
    if (node) copyText(node.innerText, el);
  },

  /* ── 成员登录 / 身份（v1.18.x） ───────────────────────── */
  // openLoginModal(prefillName)：统计页「修改信息」带姓名、「登录」不带
  "open-login-modal": (event, el) => openLoginModal(el.dataset.prefillName || ""),
  "logout": () => logout(),
  "select-identity": (event, el) => selectIdentity(el.dataset.identity),
  "skip-member-name": () => skipMemberName(),
  "submit-session": () => submitSession(),
};

document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const handler = ACTIONS[el.dataset.action];
  if (handler) handler(event, el);
});

/* tab 键盘导航：内联 onkeydown 下线后由 bar 级委托接管（handleTabKey 内部已适配） */
const tabBar = document.querySelector(".tab-bar");
if (tabBar) tabBar.addEventListener("keydown", handleTabKey);

/* 输入框：oninput 内联属性下线后改为直接绑定 */
const input = document.getElementById("inputUrls");
if (input) input.addEventListener("input", onInputChange);

/* 历史搜索框：同上（onHistorySearch 接收 input 元素本身） */
const historySearch = document.getElementById("historySearch");
if (historySearch) historySearch.addEventListener("input", () => onHistorySearch(historySearch));

/* 登录框两个输入框的回车提交（原内联 onkeydown="if(event.key==='Enter')submitSession()"） */
["memberNameInput", "memberPinInput"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submitSession();
  });
});
