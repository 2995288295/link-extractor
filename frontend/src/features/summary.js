import { apiFetch } from "../core/api.js";

/**
 * 首页小提示：本月有效条数（2026-10-01 口径）。
 * 有效 = 本月成功提取的记录按规范链接去重；抖音 1 条、小红书 0.5 条、视频号 0.5 条。
 * 拉取失败静默忽略——提示条只是辅助信息，不阻塞主功能。
 */
export async function showMonthlySummary() {
  let data;
  try {
    data = await apiFetch("/api/monthly-summary");
  } catch (e) {
    return;
  }
  if (!data || !data.success) return;

  const c = data.counts || {};
  const total = Number(data.valid_count) || 0;
  const parts = [];
  if (c.douyin) parts.push(`抖音 ${c.douyin}`);
  if (c.xiaohongshu) parts.push(`小红书 ${c.xiaohongshu}`);
  if (c.sph) parts.push(`视频号 ${c.sph}`);
  const detail = parts.length ? parts.join(" · ") : "本月还没有有效记录";

  const tip = document.createElement("div");
  tip.className = "monthly-tip";
  tip.setAttribute("role", "status");
  tip.innerHTML = `
    <button class="monthly-tip-close" type="button" aria-label="关闭">×</button>
    <div class="monthly-tip-title">📊 本月有效条数：<strong>${total}</strong> 条</div>
    <div class="monthly-tip-detail">${detail}（同一内容已去重）</div>
    <div class="monthly-tip-rule">计法：抖音 1 条 · 小红书 0.5 条 · 视频号 0.5 条</div>`;
  tip.querySelector(".monthly-tip-close").addEventListener("click", () => tip.remove());
  document.body.appendChild(tip);

  // 10 秒自动收起；:hover 时暂停由 CSS 难以实现，保持简单
  setTimeout(() => {
    tip.classList.add("leaving");
    setTimeout(() => tip.remove(), 350);
  }, 10000);
}
