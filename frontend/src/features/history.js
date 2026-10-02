import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { esc } from "../core/dom.js";
import { showToast } from "../ui/toast.js";
import { coverPreviewHtml, initCaptionToggles } from "./results.js";

// ---------------------------------------------------------------- 历史

/* 本地筛选状态（v1.14.0）：接口一次拉 200 条，筛选/搜索在浏览器侧完成 */
let lastItems = [];
const historyFilter = { platform: "all", keyword: "" };

export function setHistoryFilter(platform) {
  historyFilter.platform = platform;
  document.querySelectorAll("#historyPlatformChips button").forEach(b => {
    const active = b.dataset.platform === platform;
    b.classList.toggle("btn-primary", active);
    b.classList.toggle("btn-ghost", !active);
  });
  renderHistoryList();
}

export function onHistorySearch(input) {
  historyFilter.keyword = (input.value || "").trim().toLowerCase();
  renderHistoryList();
}

function filteredItems() {
  const kw = historyFilter.keyword;
  return lastItems.filter(h => {
    if (historyFilter.platform !== "all" && (h.platform || "") !== historyFilter.platform) return false;
    if (kw) {
      const hay = `${h.title || ""} ${h.caption || ""} ${h.original_url || ""} ${h.canonical_url || ""}`.toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
}

export async function loadHistory() {
  const list = document.getElementById("historyList");
  const status = document.getElementById("historyStatus");
  status.textContent = "加载中...";
  try {
    const data = await apiFetch("/api/history");
    lastItems = data.items || [];
    renderHistoryList();
  } catch (e) {
    status.textContent = "加载历史失败";
  }
}

function renderHistoryList() {
  const list = document.getElementById("historyList");
  const status = document.getElementById("historyStatus");
  const filtered = filteredItems();
  const isFiltering = historyFilter.platform !== "all" || historyFilter.keyword !== "";
  status.textContent = isFiltering
    ? `筛选出 ${filtered.length} / ${lastItems.length} 条（仅自己可见）`
    : (lastItems.length ? `共 ${lastItems.length} 条历史记录（仅自己可见）` : "暂无历史记录");
  if (!lastItems.length) {
    list.innerHTML = '<div class="card" style="color:var(--text2);text-align:center;">暂无历史记录</div>';
    return;
  }
  if (!filtered.length) {
    list.innerHTML = '<div class="card" style="color:var(--text2);text-align:center;">当前筛选条件下没有记录</div>';
    return;
  }
  list.innerHTML = filtered.map((h, i) => `
    <div class="card" style="padding:14px 16px;">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;">
        <div style="font-size:13px;color:var(--text2);">#${filtered.length - i} · ${esc(h.platform || "未知平台")} · ${esc(h.created_at)}</div>
        ${h.status === "success"
          ? '<span class="result-badge badge-ok">成功</span>'
          : '<span class="result-badge badge-fail">失败</span>'}
      </div>
      <div class="history-card-body">
        ${h.status === "success" && h.cover_url ? `<div class="cover-wrap">${coverPreviewHtml(h.cover_url, h.title || h.caption || h.platform || "内容", h.platform === "小红书" ? "xiaohongshu" : h.platform === "抖音" ? "douyin" : h.platform === "视频号" ? "sph" : "")}</div>` : ""}
        <div class="history-card-content">
          ${h.status === "success"
            ? `
          <div class="field" style="margin-top:8px;">
            <div class="field-label">转换链接</div>
            <div class="field-value link" title="${esc(h.canonical_url || h.original_url)}">${esc(h.canonical_url || h.original_url)}</div>
          </div>
          ${h.caption ? `<div class="field"><div class="field-label">文案</div><div class="caption-wrap"><div class="field-value caption">${esc(h.caption)}</div><button class="caption-toggle" type="button" data-action="toggle-caption">展开全部 ▼</button></div></div>` : ""}
          <div class="copy-row">
            <button class="btn btn-sm btn-copy" data-action="copy-history-link">复制链接</button>
            ${h.caption ? `<button class="btn btn-sm btn-copy" data-action="copy-history-caption">复制文案</button>` : ""}
          </div>`
            : `
          <div class="field" style="margin-top:8px;">
            <div class="field-label">原始链接</div>
            <div class="field-value link" title="${esc(h.original_url)}">${esc(h.original_url)}</div>
          </div>
          <div class="field"><div class="field-label">错误</div><div class="field-value" style="color:var(--error);">${esc(h.error)}</div></div>`}
        </div>
      </div>
    </div>
  `).join("");
  initCaptionToggles(list);
}

export async function clearHistory() {
  const btn = document.getElementById("btnClearHistory");
  // 二次确认：第一次点击进入确认态，3 秒内再点才真正执行
  if (!btn.classList.contains("confirming")) {
    btn.classList.add("confirming");
    btn.textContent = "⚠ 再点一次确认清空";
    clearTimeout(state.clearArmTimer);
    state.clearArmTimer = setTimeout(() => {
      btn.classList.remove("confirming");
      btn.textContent = "🗑 清空历史";
    }, 3000);
    return;
  }
  clearTimeout(state.clearArmTimer);
  btn.classList.remove("confirming");
  btn.textContent = "🗑 清空历史";
  try {
    await apiFetch("/api/history", { method: "DELETE" });
    showToast("历史已清空");
    loadHistory();
  } catch (e) {
    showToast("清空失败");
  }
}
