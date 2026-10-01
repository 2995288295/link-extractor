import { apiFetch } from "../core/api.js";

/**
 * 本月有效条数（2026-10-01 口径）。
 * 有效 = 本月成功提取的记录按规范链接去重；抖音 1 条、小红书 0.5 条、视频号 0.5 条。
 *
 * v1.16.0 起不再自动弹 toast 条（曾与姓名弹窗相互遮挡、还被吐槽像「飘浮广告」），
 * 改为把数字渲染进引导弹窗顶部的常驻区，统计页另有完整卡片。
 */
export async function fetchMonthlySummary() {
  try {
    return await apiFetch("/api/monthly-summary");
  } catch (e) {
    return null;
  }
}

export function monthlySummaryHtml(data) {
  if (!data || !data.success) return "";
  const c = data.counts || {};
  const total = Number(data.valid_count) || 0;
  const parts = [];
  if (c.douyin) parts.push(`抖音 ${c.douyin}`);
  if (c.xiaohongshu) parts.push(`小红书 ${c.xiaohongshu}`);
  if (c.sph) parts.push(`视频号 ${c.sph}`);
  const detail = parts.length ? parts.join(" · ") : "本月还没有有效记录";
  return `
    <div class="monthly-banner">
      <div class="monthly-banner-num"><span>${total}</span> 条</div>
      <div class="monthly-banner-text">
        <div class="monthly-banner-title">本月有效条数</div>
        <div class="monthly-banner-sub">${detail}（同一内容已去重）· 计法：抖音 1 条 · 小红书 0.5 条 · 视频号 0.5 条</div>
      </div>
    </div>`;
}

/** 兼容旧调用点：页面加载时不再弹条（保留导出但不再使用）。 */
export async function showMonthlySummary() {
  return null;
}
