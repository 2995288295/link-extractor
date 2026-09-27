import { apiFetch } from "../core/api.js";
import { esc } from "../core/dom.js";

// ---------------------------------------------------------------- 统计

export async function loadStats() {
  try {
    const data = await apiFetch("/api/stats");
    renderStats(data);
  } catch (e) {
    document.getElementById("statsGrid").innerHTML =
      '<div class="card" style="color:var(--error);text-align:center;">统计加载失败</div>';
  }
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