import { adminFetch, fmt, logout, showToast } from "./core.js";
import { state } from "./state.js";
import { renderDevices, renderErrors, renderRecent } from "./render-tables.js";
import { renderTrend, renderPlatform, renderPlatformHealth, renderServiceStatus, renderAlerts } from "./render-metrics.js";
import { loadPool } from "./pool.js";

/* ===== 核心指标：随时间变 → 由轮询刷新（3 请求） ===== */
export async function loadCore() {
  const range = document.getElementById("rangeSelect").value;
  const q = `?range=${encodeURIComponent(range)}`;
  const [ov, perf, healthRes] = await Promise.all([
    adminFetch("/api/admin/overview" + q),
    adminFetch("/api/admin/performance" + q),
    fetch("/api/health").then(r => r.json()).catch(() => null),
  ]);
    document.getElementById("mTotal").textContent = fmt(ov.total);
    document.getElementById("mRate").textContent = ov.success_rate + "%";
    document.getElementById("mFail").textContent = `失败 ${fmt(ov.fail)} · 服务稳定性 ${ov.service_success_rate ?? ov.success_rate}%`;
    document.getElementById("mDevices").textContent = fmt(ov.active_devices);
    document.getElementById("mDevicesSub").textContent = `7天 ${fmt(ov.active_devices_7d)} · 30天 ${fmt(ov.active_devices_30d)}`;
    document.getElementById("mToday").textContent = fmt(ov.today_count);
    const trend = ov.trend || []; const last = Number(trend.at(-1)?.count || 0), prev = Number(trend.at(-2)?.count || 0); const delta = prev ? ((last-prev)/prev*100).toFixed(1) : "0.0"; const trendEl = document.getElementById("mTotalTrend"); trendEl.textContent = `${delta >= 0 ? "↑" : "↓"} ${Math.abs(delta)}% 较上一周期`; trendEl.className = `trend ${delta > 0 ? "up" : delta < 0 ? "down" : "flat"}`;
    document.getElementById("pAvg").textContent = fmt(perf.avg_duration_ms);
    document.getElementById("p95").textContent = fmt(perf.p95_duration_ms);
    document.getElementById("pHit").textContent = perf.cache_hit_rate + "%";
    document.getElementById("pBatch").textContent = perf.batch_concurrency + " / " + perf.queue_concurrency;
    document.getElementById("pQueue").textContent = "队列并发 " + perf.queue_concurrency;
    const retry = perf.douyin_retry_success_attempts || {};
    const retryParts = ["1", "2", "3"].map(n => {
      const v = retry[n];
      return `${n}次成功 ${v ? fmt(v.count) + "（" + v.rate + "%）" : "0（0%）"}`;
    });
    document.getElementById("retryInsight").textContent = "抖音成功尝试分布：" + retryParts.join(" · ");
    document.getElementById("updatedAt").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit", second:"2-digit"});
    renderTrend(ov.trend);
    renderPlatform(ov.platform_dist);
    renderPlatformHealth(ov.platform_health);
    renderServiceStatus(healthRes, ov, perf);
    renderAlerts(ov, perf, healthRes);
}

/* ===== 明细列表：变化慢 → 只在全量刷新时拉（3 请求） ===== */
export async function loadDetails() {
  const range = document.getElementById("rangeSelect").value;
  const q = `?range=${encodeURIComponent(range)}`;
  const [dev, errs, recent] = await Promise.all([
    adminFetch("/api/admin/devices" + q + "&limit=100"),
    adminFetch("/api/admin/errors" + q),
    adminFetch("/api/admin/recent" + q),
  ]);
  state.deviceCache = dev.devices || [];
  state.recentCache = recent.items || [];
  state.lastErrors = errs.errors || [];
  state.lastErrorKinds = errs.error_kinds || [];
  renderDevices(state.deviceCache);        // 内部自己读搜索框 / 排序，行为与改前一致
  renderErrors(state.lastErrors, state.lastErrorKinds);
  renderRecent(state.recentCache);
}

/* 轮询 / 回到前台用：失败静默，仅 401 才登出（避免未捕获的 Promise rejection） */
export function refreshCore() {
  loadCore().catch(e => {
    if (e && e.message === "unauthorized") { logout(); showToast("请重新登录"); }
  });
}

/* ===== 全量刷新：首次进入 / 手动「↻ 刷新」/ 切换统计范围 ===== */
export async function loadAll() {
  try {
    const range = document.getElementById("rangeSelect").value;
    document.getElementById("trendTitle").textContent = range === "24h" ? "最近 24 小时提取" : range === "30d" ? "近 30 天提取趋势" : "近 7 天提取趋势";
    await Promise.all([loadCore(), loadDetails()]);
    if (currentView() === "pool") loadPool();
  } catch (e) {
    if (e.message === "unauthorized") { logout(); showToast("请重新登录"); }
    else showToast("加载失败，请点击刷新重试");
  }
}

export function currentView() {
  return document.querySelector(".nav-tab.active")?.dataset.view || "overview";
}