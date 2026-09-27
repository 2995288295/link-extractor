// 注意：本模块与 extract.js 存在**函数级循环引用**
// （onInputChange -> doExtract；doExtract -> extractUrls/updateLinkCount）。
// ESM 的函数声明是 hoisted 且绑定是活的，调用都发生在运行时，故安全。
// 两个模块顶层都没有副作用语句，Rollup 打包不受影响（已在隔离验证中实测）。
import { state } from "../core/state.js";
import { doExtract } from "./extract.js";

export function clearInput() {
  document.getElementById("inputUrls").value = "";
  document.getElementById("extractStatus").textContent = "";
  document.getElementById("results").innerHTML = "";
  state.lastAutoValue = "";
  clearTimeout(state.autoExtractTimer);
  updateLinkCount();
}

// ---------------------------------------------------------------- 链接识别

/** 从文本中提取所有 URL（与后端正则一致） */
export function extractUrls(text) {
  const re = /https?:\/\/[^\s\u4e00-\u9fff]+/g;
  const found = (text || "").match(re) || [];
  const seen = new Set();
  return found
    .map(u => u.trim().replace(/[.,;:!?，。；：！？）】》]+$/g, ""))
    .filter(u => u && !seen.has(u) && seen.add(u));
}

/** 实时统计输入框中的链接数 */
export function updateLinkCount() {
  const raw = document.getElementById("inputUrls").value;
  const count = extractUrls(raw).length;
  const el = document.getElementById("linkCount");
  if (count === 0) {
    el.innerHTML = "";
  } else {
    el.innerHTML = `🔗 已识别 <span class="count">${count}</span> 条链接` + (count > 20 ? '<span style="color:var(--error);margin-left:8px;">（超过 20 条上限，请分批）</span>' : "");
  }
}

/** 输入变化：更新计数 + 防抖 1 秒自动提取 */
export function onInputChange() {
  updateLinkCount();
  clearTimeout(state.autoExtractTimer);
  const raw = document.getElementById("inputUrls").value;
  if (state.extractingNow || !raw.trim()) return;
  state.autoExtractTimer = setTimeout(() => {
    if (raw !== state.lastAutoValue && extractUrls(raw).length) {
      doExtract(true);
    }
  }, 1000);
}