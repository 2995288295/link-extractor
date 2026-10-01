import { apiFetch } from "../core/api.js";
import { esc } from "../core/dom.js";
import { state } from "../core/state.js";

// ---------------------------------------------------------------- 统计

export async function loadStats() {
  try {
    const [data, monthly, profile] = await Promise.all([
      apiFetch("/api/stats"),
      apiFetch("/api/monthly-summary").catch(() => null),
      apiFetch("/api/profile").catch(() => null),
    ]);
    renderStats(data);
    renderMonthlyCard(monthly, profile);
  } catch (e) {
    document.getElementById("statsGrid").innerHTML =
      '<div class="card" style="color:var(--error);text-align:center;">统计加载失败</div>';
  }
}

/** 本月有效条数常驻卡片（v1.14.0）：与首页弹窗同口径，长期可见 */
function renderMonthlyCard(monthly, profile) {
  const el = document.getElementById("monthlyCard");
  if (!el) return;
  if (!monthly || !monthly.success) {
    el.innerHTML = '<div style="color:var(--text2);font-size:13px;">本月统计暂时不可用</div>';
    return;
  }
  const c = monthly.counts || {};
  const serverName = (profile && profile.name) || "";
  const name = serverName || state.memberName || "";
  const nameHtml = name
    ? `<span style="font-weight:600;">${esc(name)}</span> <button class="btn btn-sm btn-ghost" onclick="openNameModal()">改名</button>`
    : `<button class="btn btn-sm btn-primary" onclick="openNameModal()">设置姓名</button>`;
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
      <h2 style="margin:0;">本月有效条数：<span style="color:var(--primary);font-size:24px;">${monthly.valid_count}</span> 条</h2>
      <div>${nameHtml}</div>
    </div>
    <div style="color:var(--text2);font-size:13px;margin-top:8px;">
      抖音 ${c.douyin || 0} · 小红书 ${c.xiaohongshu || 0} · 视频号 ${c.sph || 0}（同一内容已去重）
      ｜ 计法：抖音 1 条 · 小红书 0.5 条 · 视频号 0.5 条
    </div>`;
}

export function renderStats(s) {
  // 顶部指标卡
  const platformChips = Object.entries(s.platform_dist || {})
    .map(([name, cnt]) => `<span class="platform-chip">${esc(name)} ${cnt} 条</span>`)
    .join("");
  const grid = document.getElementById("statsGrid");
  grid.innerHTML = `
    <div class="stat-card ok">
      <div class="num">${s.total}</div>
      <div class="label">累计提取</div>
      <div class="sub">${s.ok} 成功 · ${s.fail} 失败</div>
    </div>
    <div class="stat-card">
      <div class="num">${s.success_rate}%</div>
      <div class="label">成功率</div>
      <div class="sub">${s.total ? "最近 200 条内统计" : "暂无数据"}</div>
    </div>
    <div class="card" style="grid-column: 1 / -1;">
      <h2 style="margin-bottom:4px;">平台分布</h2>
      <div class="platform-chips">${platformChips || '<span style="color:var(--text2);font-size:13px;">暂无成功提取记录</span>'}</div>
    </div>`;

  // 趋势折线图（SVG）
  const trend = document.getElementById("statsTrend");
  const hasData = s.trend && s.trend.some(t => t.count > 0);
  if (!hasData) {
    trend.innerHTML = '<div style="color:var(--text2);font-size:13px;padding:20px 0;">近 7 天暂无提取记录</div>';
    return;
  }
  const W = 760, H = 160, padL = 34, padR = 10, padT = 22, padB = 24;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const max = Math.max(...s.trend.map(t => t.count), 1);
  const n = s.trend.length;
  const x = i => padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = v => padT + plotH - (v / max) * plotH;

  // 网格线（3 条水平参考线）+ Y 轴刻度
  let trendGrid = "";
  for (let g = 0; g <= 2; g++) {
    const val = Math.round((max / 2) * g);
    const gy = y(val);
    trendGrid += `<line class="grid-line" x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}"/>`;
    trendGrid += `<text class="axis-label" x="${padL - 6}" y="${gy + 3}" text-anchor="end">${val}</text>`;
  }

  // 折线 path + 面积渐变底色
  const points = s.trend.map((t, i) => `${x(i)},${y(t.count)}`);
  const linePath = "M" + points.join(" L");
  const areaPath = `${linePath} L${x(n - 1)},${padT + plotH} L${x(0)},${padT + plotH} Z`;

  const dots = s.trend.map((t, i) => `
    <circle class="dot" cx="${x(i)}" cy="${y(t.count)}" r="4">
      <title>${esc(t.date)}：${t.count} 条</title>
    </circle>
    <text class="value-label" x="${x(i)}" y="${y(t.count) - 9}" text-anchor="middle">${t.count}</text>
    <text class="axis-label" x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(t.date)}</text>`).join("");

  trend.innerHTML = `
    <svg class="trend-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      ${trendGrid}
      <path d="${areaPath}" fill="var(--primary)" opacity="0.08"/>
      <path d="${linePath}" fill="none" stroke="var(--primary)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${dots}
    </svg>`;
}