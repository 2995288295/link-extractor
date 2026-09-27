// 依赖方向：ui -> features（features 层不反向依赖 ui 层，无环）
import { loadStats } from "../features/stats.js";
import { loadHistory } from "../features/history.js";

export function switchTab(tab) {
  document.querySelectorAll(".tab").forEach(t => {
    const active = t.dataset.tab === tab;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
    t.tabIndex = active ? 0 : -1;
  });
  document.getElementById("tab-extract").classList.toggle("hidden", tab !== "extract");
  document.getElementById("tab-stats").classList.toggle("hidden", tab !== "stats");
  document.getElementById("tab-history").classList.toggle("hidden", tab !== "history");
  if (tab === "stats") loadStats();
  if (tab === "history") loadHistory();
}

export function handleTabKey(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll(".tab")];
  const current = tabs.indexOf(event.currentTarget);
  let next = current;
  if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
  if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = tabs.length - 1;
  event.preventDefault();
  tabs[next].focus();
  switchTab(tabs[next].dataset.tab);
}