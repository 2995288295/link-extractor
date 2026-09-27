import { esc, fmt, showToast } from "./core.js";

export function renderTrend(trend) {
  const el = document.getElementById("trendChart");
  if (!trend || !trend.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const max = Math.max(...trend.map(t => t.count), 1);
  const w = 360, h = 110, pad = 18;
  const step = (w - pad * 2) / Math.max(trend.length - 1, 1);
  const pts = trend.map((t, i) => {
    const x = pad + i * step;
    const y = h - pad - (t.count / max) * (h - pad * 2);
    return [x, y];
  });
  const line = pts.map(p => p.join(",")).join(" ");
  const okPts = trend.map((t, i) => {
    const x = pad + i * step;
    const y = h - pad - ((t.ok || 0) / max) * (h - pad * 2);
    return [x, y];
  });
  const dots = pts.map((p, i) =>
    `<circle data-trend-index="${i}" cx="${p[0]}" cy="${p[1]}" r="4" fill="#1769e0" style="cursor:crosshair"><title>${trend[i].date} 共${trend[i].count}条 成功${trend[i].ok}</title></circle>`
  ).join("");
  el.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;">
      <line x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}" stroke="#dbe3ee" stroke-width="1"/>
      <polyline points="${line}" fill="none" stroke="#1769e0" stroke-width="2"/>
      <polyline points="${okPts.map(p => p.join(",")).join(" ")}" fill="none" stroke="#18a866" stroke-width="2" stroke-dasharray="4 3"/>
      ${dots}
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text2);">
      ${trend.map(t => `<span>${esc(t.date)}</span>`).join("")}
    </div><div style="font-size:11px;color:var(--text2);margin-top:3px;">蓝线 总提取 · 绿虚线 成功</div>`;
  const tip = document.getElementById("chartTip");
  el.querySelectorAll("circle[data-trend-index]").forEach(dot => {
    dot.addEventListener("mouseenter", () => {
      const item = trend[Number(dot.dataset.trendIndex)];
      tip.innerHTML = `<strong>${esc(item.date)}</strong><br>提取 ${fmt(item.count)} 条 · 成功 ${fmt(item.ok)}<br>失败 ${fmt(item.fail ?? item.count - item.ok)} 条`;
      const wrap = tip.parentElement.getBoundingClientRect();
      const rect = dot.getBoundingClientRect();
      tip.style.left = `${Math.max(4, rect.left - wrap.left - 28)}px`;
      tip.style.top = `${Math.max(2, rect.top - wrap.top - 62)}px`;
      tip.style.display = "block";
    });
    dot.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });
}

export function renderPlatform(dist) {
  const el = document.getElementById("platformDist");
  const rows = Object.entries(dist || {});
  if (!rows.length) { el.innerHTML = '<div class="empty">暂无平台数据</div>'; return; }
  const total = rows.reduce((n, [,v]) => n + Number(v || 0), 0);
  const names = { douyin:"抖音", xiaohongshu:"小红书", "抖音":"抖音", "小红书":"小红书" };
  el.innerHTML = rows.map(([k,v]) => { const pct = total ? (Number(v)/total*100).toFixed(1) : 0; return `<div class="platform-card"><div class="platform-name">${esc(names[k] || k)}</div><div class="platform-num">${fmt(v)}</div><div class="platform-meta"><span>成功提取</span><b>${pct}%</b></div><div class="bar" style="margin-top:7px"><i class="${k === "xiaohongshu" || k === "小红书" ? "xhs" : "douyin"}" style="width:${pct}%"></i></div></div>`; }).join("");
}
export function renderPlatformHealth(health) {
  const el = document.getElementById("platformHealth");
  const entries = Object.entries(health || {});
  if (!entries.length) { el.innerHTML = '<div class="empty">暂无平台数据</div>'; return; }
  el.innerHTML = entries.map(([name, v]) => {
    const rate = v.service_success_rate ?? v.success_rate;
    const cls = rate >= 95 ? "ok" : rate >= 85 ? "" : "fail";
    return `<div class="health-row"><span>${esc(name)} <span style="color:var(--text2);font-size:12px;">${fmt(v.total)} 次 · 用户错误 ${fmt(v.user_errors)} · 服务失败 ${fmt(v.service_failures)}</span></span><span class="health-rate ${cls}">${rate}%</span></div>`;
  }).join("");
}

export function renderServiceStatus(health, ov, perf) {
  const strip = document.getElementById("statusStrip");
  const text = document.getElementById("statusText");
  const rate = Number(ov.service_success_rate ?? ov.success_rate ?? 0);
  const p95 = Number(perf.p95_duration_ms || 0);
  const uptimeLabel = health && Number.isFinite(Number(health.uptime_seconds)) ? formatUptime(health.uptime_seconds) : "未连接";
  document.getElementById("headerUptime").textContent = `运行 ${uptimeLabel}`;
  let state = "ok";
  strip.className = "status-strip";
  if (!health || health.status !== "ok") {
    state = "bad"; strip.classList.add("bad"); text.textContent = "服务异常：健康检查未通过";
  } else if (rate < 90 || p95 > 5000) {
    state = "warn"; strip.classList.add("warn"); text.textContent = `需要关注：服务稳定性 ${rate}% · P95 ${fmt(p95)}ms`;
  } else {
    const uptime = health && Number.isFinite(Number(health.uptime_seconds)) ? ` · 运行 ${formatUptime(health.uptime_seconds)}` : "";
    text.textContent = `服务正常：稳定性 ${rate}% · P95 ${fmt(p95)}ms${uptime}`;
  }
  document.title = state === "ok" ? "运营看板 · 链接提取工具" : "⚠️ 运营看板 · 需要关注";
  if (window._lastDashboardState && window._lastDashboardState !== state && state !== "ok") {
    showToast(state === "bad" ? "服务异常，请立即检查" : "服务指标需要关注");
  }
  window._lastDashboardState = state;
}

export function formatUptime(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}天${h}小时` : h ? `${h}小时${m}分` : `${m}分`;
}

export function renderAlerts(ov, perf, health) {
  const panel = document.getElementById("alertPanel");
  const summary = document.getElementById("alertSummary");
  const alerts = [];
  const rate = Number(ov.service_success_rate ?? ov.success_rate ?? 0);
  const p95 = Number(perf.p95_duration_ms || 0);
  if (!health || health.status !== "ok") alerts.push({kind:"bad", title:"服务健康检查未通过", detail:"请立即检查 5003 服务进程与网络状态。"});
  if (rate > 0 && rate < 90) alerts.push({kind:"bad", title:`服务成功率 ${rate}%`, detail:"低于 90% 运营阈值，建议优先查看失败原因 TOP。"});
  if (p95 > 5000) alerts.push({kind:"warn", title:`P95 耗时 ${fmt(p95)}ms`, detail:"响应速度偏慢，建议检查平台风控、重试和队列并发。"});
  if (!alerts.length) { summary.textContent = "当前无高优先级异常"; panel.innerHTML = '<div class="alert-empty">✓ 服务指标处于安全区间，暂无需要立即处理的问题</div>'; return; }
  summary.textContent = `${alerts.length} 项需要关注`;
  panel.innerHTML = alerts.map(a => `<div class="alert-item ${a.kind === "warn" ? "warn" : ""}"><div class="alert-icon">${a.kind === "warn" ? "!" : "×"}</div><div><div class="alert-title">${esc(a.title)}</div><div class="alert-detail">${esc(a.detail)}</div></div></div>`).join("");
}