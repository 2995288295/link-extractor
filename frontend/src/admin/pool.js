import { adminFetch, esc, fmt, logout, readCookie, showToast } from "./core.js";
import { state } from "./state.js";

/* ================= v2.2 代理池运维（爱加速换 IP 兜底） ================= */
let poolState = null;

export async function adminPost(url, body) {
  const headers = { "Content-Type": "application/json" };
  if (state.adminToken) headers["X-Admin-Token"] = state.adminToken;
  const csrf = readCookie("csrf_token");
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const res = await fetch(url, {
    method: "POST", headers, credentials: "same-origin",
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401 || res.status === 503) throw new Error("unauthorized");
  const json = await res.json().catch(() => ({}));
  if (!json.success) throw new Error(json.error || "操作失败");
  return json;
}

export function poolPill(text, kind) { return '<span class="pool-pill ' + kind + '">' + esc(text) + "</span>"; }

export function poolAccountStatus(a) {
  if (!a.enabled) return poolPill("已停用", "off");
  if (a.exhausted) return poolPill(a.exhaustedReason || "额度用尽", "bad");
  if ((a.consecutiveFails || 0) >= 3) return poolPill("连续失败 " + a.consecutiveFails, "warn");
  if ((a.leases || 0) > 0) return poolPill("正常", "on");
  return poolPill("未使用", "off");
}

export async function loadPool() {
  try {
    const data = await adminFetch("/api/admin/pool/status");
    poolState = data;
    renderPoolStrip(data);
    renderPoolTable(data);
    renderPoolTrend(data.riskTrend || []);
    renderPoolEvents(data.recentEvents || []);
  } catch (e) {
    if (e.message === "unauthorized") { logout(); showToast("请重新登录"); return; }
    document.getElementById("poolStripText").textContent = "代理池读取失败：" + e.message;
  }
}

export function renderPoolStrip(data) {
  const p = data.pool || {}, cfg = data.config || {}, fr = p.freeze || {};
  const strip = document.getElementById("poolStrip");
  const on = !!p.failoverEnabled, avail = !!data.poolAvailable;
  const busy = !!p.proxyPortBusy, sheapi = !!p.sheapiCycleRunning;
  strip.classList.remove("warn", "bad");
  if (!avail) strip.classList.add("bad");
  else if (on) strip.classList.add("warn");

  const bits = [];
  bits.push("<b>兜底：" + (on ? "已开启" : "已关闭") + "</b>");
  if (avail) {
    bits.push("池剩余 " + fmt(p.poolRemainingSeconds) + "s / " + fmt(p.poolBudgetSeconds) + "s");
    bits.push("账号 " + (p.accounts || []).length + " 个");
  } else {
    bits.push("代理池不可用");
  }
  bits.push("1080" + (busy ? "占用中" : "空闲"));
  if (sheapi) bits.push("签到轮换进行中");
  if (fr.enabled) {
    bits.push("冻结窗口 " + esc(fr.start) + "–" + esc(fr.end) + (fr.frozen ? "（当前生效，不切IP）" : ""));
  }
  const lease = p.lease || {};
  if (lease.active) {
    bits.push("当前租约 " + esc(lease.exitIp || "-") + "（" + esc(lease.reason || "-") + "）");
  } else {
    bits.push("无活动租约");
  }
  document.getElementById("poolStripText").innerHTML = bits.join(" · ");

  const btn = document.getElementById("poolFailoverBtn");
  btn.textContent = on ? "关闭兜底" : "开启兜底";
  btn.disabled = !avail;
  document.getElementById("poolSubtitle").textContent =
    "爱加速 · 换 IP 兜底" + (on ? "（开启）" : "（关闭）") + (fr.frozen ? " · 冻结中" : "");
}

export function renderPoolTable(data) {
  const body = document.getElementById("poolBody");
  const accounts = (data.pool && data.pool.accounts) || [];
  if (!accounts.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">还没有登记任何爱加速账号</td></tr>';
    return;
  }
  body.innerHTML = accounts.map(a => {
    const pct = a.dailySeconds ? Math.min(100, Math.round((a.usedSeconds / a.dailySeconds) * 100)) : 0;
    const usedTitle = esc("额度 " + a.dailySeconds + "s · 已用 " + a.usedSeconds + "s（" + pct + "%）" +
      (a.reserveSeconds ? " · 预留 " + a.reserveSeconds + "s" : "") +
      (a.leases ? " · 租约 " + a.leases + " 次" : "") +
      (a.lastUsed ? " · 最近 " + a.lastUsed : ""));
    return "<tr>" +
      '<td><b>' + esc(a.name) + '</b>' + (a.note ? '<div class="updated">' + esc(a.note) + '</div>' : "") + "</td>" +
      "<td>" + (a.enabled ? poolPill("启用", "on") : poolPill("停用", "off")) + "</td>" +
      '<td class="num" title="' + usedTitle + '">' + fmt(a.usedSeconds) + " / " + fmt(a.dailySeconds) + "</td>" +
      '<td class="num"><b>' + fmt(a.remainingSeconds) + "</b></td>" +
      '<td class="num">' + fmt(a.reserveSeconds) + "</td>" +
      "<td>" + poolAccountStatus(a) + "</td>" +
      '<td><div class="pool-actions">' +
        '<button class="pool-mini" data-pool-act="check" data-name="' + esc(a.name) + '">轻检</button>' +
        '<button class="pool-mini" data-pool-act="deepcheck" data-name="' + esc(a.name) + '">深检</button>' +
        '<button class="pool-mini" data-pool-act="edit" data-name="' + esc(a.name) + '">编辑</button>' +
        '<button class="pool-mini danger" data-pool-act="remove" data-name="' + esc(a.name) + '">移除</button>' +
      "</div></td>" +
      "</tr>";
  }).join("");
}

export function renderPoolTrend(trend) {
  const el = document.getElementById("poolRiskTrend");
  if (!trend.length) { el.innerHTML = '<div class="updated">暂无数据</div>'; return; }
  const max = Math.max(1, ...trend.map(d => d.risk || 0));
  el.innerHTML = trend.map(d => {
    const risk = d.risk || 0, h = Math.max(2, Math.round((risk / max) * 56));
    const calm = risk === 0 ? " calm" : "";
    const tip = esc(d.day + " · 请求 " + d.total + " · 失败 " + d.fail + " · 风控 " + risk);
    return '<div class="bar-col' + calm + '" title="' + tip + '"><i style="height:' + h + 'px"></i><small>' +
      esc(String(d.day).slice(5)) + "</small></div>";
  }).join("");
}

export function renderPoolEvents(events) {
  const el = document.getElementById("poolEvents");
  if (!events.length) { el.innerHTML = '<div class="updated">暂无事件</div>'; return; }
  el.innerHTML = events.map(ev => '<div class="ev"><b>' + esc((ev.time || "").slice(11, 19)) + "</b> " + esc(ev.text || "") + "</div>").join("");
}

export function poolOutput(title, text) {
  const el = document.getElementById("poolOutput");
  el.classList.remove("hidden");
  el.textContent = title + "\n" + (text || "");
}
export function poolOutputHide() { document.getElementById("poolOutput").classList.add("hidden"); }

export async function poolToggleFailover() {
  if (!poolState) return;
  const on = !!(poolState.pool && poolState.pool.failoverEnabled);
  const want = !on;
  if (want && !confirm("开启后：小红书/抖音出现成簇风控时，会自动租一个爱加速节点换 IP 重试一次（消耗该账号的免费额度）。确认开启？")) return;
  try {
    await adminPost("/api/admin/pool/failover", { enabled: want });
    showToast(want ? "兜底已开启（热生效）" : "兜底已关闭（热生效）");
    await loadPool();
  } catch (e) { showToast(e.message); }
}

export async function poolCheckAll() { await poolCheck(""); }

export async function poolCheck(name) {
  const label = name ? ("账号 " + name) : "全部账号";
  poolOutput("正在轻检 " + label + "（验证登录 + 数免费节点，不占 1080、不耗额度）…", "");
  try {
    const r = await adminPost("/api/admin/pool/check", { name: name || "" });
    poolOutput("轻检完成 · " + label, (r.lines || []).join("\n") || (r.ok ? "通过" : "见上"));
    await loadPool();
  } catch (e) { poolOutput("轻检失败", e.message); showToast(e.message); }
}

export async function poolDeepCheck(name) {
  const label = name || "自动挑选";
  if (!confirm("深度体检会真租一个出口 IP（约 10~25s）并请求一次小红书，会占用 1080、消耗少量免费额度。继续？")) return;
  poolOutput("正在深度体检 " + label + " …", "");
  try {
    const r = await adminPost("/api/admin/pool/deepcheck", { name: name || "" });
    const res = r.result || {};
    const lines = [];
    if (res.account) lines.push("使用账号：" + res.account);
    if (res.node) lines.push("使用节点：" + res.node);
    if (res.directIp) lines.push("直连出口 IP：" + res.directIp);
    if (res.exitIp) lines.push("代理出口 IP：" + res.exitIp + (res.ipChanged ? "（已切换 ✓）" : "（未切换 ✗）"));
    if (res.probeCode !== undefined) {
      lines.push("小红书探针：http=" + res.probeCode +
        (res.probeLoginPage ? " · 跳登录页（该 IP 被标记）" : " · 未跳登录页") +
        (res.probeHasInitialState ? " · 有初始状态 ✓" : " · 无初始状态 ✗"));
    }
    if (res.probeError) lines.push("探针异常：" + res.probeError);
    if (res.elapsedSeconds !== undefined) lines.push("耗时：" + res.elapsedSeconds + "s");
    lines.push("结论：" + (res.ok ? "该出口 IP 可正常访问小红书 ✓" : "未通过 —— " + (res.message || "见上")));
    poolOutput("深度体检 · " + label + " · " + (res.ok ? "通过" : "未通过"), lines.join("\n"));
    await loadPool();
  } catch (e) { poolOutput("深度体检失败", e.message); showToast(e.message); }
}

export function togglePoolEditor() {
  const el = document.getElementById("poolCreator");
  el.classList.toggle("hidden");
  if (!el.classList.contains("hidden")) document.getElementById("pcUser").focus();
}

export async function poolCreateAccount() {
  const name = (document.getElementById("pcName").value || "").trim();
  const user = (document.getElementById("pcUser").value || "").trim();
  const password = document.getElementById("pcPassword").value || "";
  const dailySeconds = parseInt(document.getElementById("pcDaily").value || "1200", 10);
  const reserveSeconds = parseInt(document.getElementById("pcReserve").value || "0", 10);
  const note = (document.getElementById("pcNote").value || "").trim();
  if (!user || !password) { showToast("手机号和密码都不能为空"); return; }
  const label = name || user;
  poolOutput("正在登记 " + label + " 并验证登录 …", "");
  try {
    const r = await adminPost("/api/admin/pool/account", { name, user, password, dailySeconds, reserveSeconds, note });
    const finalName = (r && r.name) || label;
    ["pcName", "pcUser", "pcPassword", "pcNote"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
    document.getElementById("pcDaily").value = "1200";
    document.getElementById("pcReserve").value = "0";
    document.getElementById("poolCreator").classList.add("hidden");
    showToast("账号 " + finalName + " 已登记并通过登录验证");
    poolOutput("登记成功 · " + finalName, "登录验证通过。可用「深度体检」确认该账号的出口 IP 是否被小红书放行。");
    await loadPool();
  } catch (e) { poolOutput("登记失败 · " + label, e.message); showToast(e.message); }
}

export function openAccountEdit(name) {
  const a = ((poolState && poolState.pool && poolState.pool.accounts) || []).find(x => x.name === name);
  if (!a) return;
  document.getElementById("peName").textContent = name;
  document.getElementById("peEnabled").checked = !!a.enabled;
  document.getElementById("peDaily").value = a.dailySeconds;
  document.getElementById("peReserve").value = a.reserveSeconds;
  document.getElementById("peNote").value = a.note || "";
  document.getElementById("pePassword").value = "";
  document.getElementById("poolEditor").classList.remove("hidden");
  document.getElementById("poolEditor").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

export async function saveAccountEdit() {
  const name = document.getElementById("peName").textContent;
  const enabled = document.getElementById("peEnabled").checked;
  const dailySeconds = parseInt(document.getElementById("peDaily").value || "1200", 10);
  const reserveSeconds = parseInt(document.getElementById("peReserve").value || "0", 10);
  const note = document.getElementById("peNote").value || "";
  const password = document.getElementById("pePassword").value || "";
  try {
    await adminPost("/api/admin/pool/account/update", { name, enabled, dailySeconds, reserveSeconds, note });
    if (password) {
      const r = await adminPost("/api/admin/pool/account/password", { name, password });
      showToast(r.message || "密码已更新");
    } else {
      showToast("已保存 " + name);
    }
    document.getElementById("poolEditor").classList.add("hidden");
    await loadPool();
  } catch (e) { showToast(e.message); }
}

export async function poolRemoveAccount(name) {
  if (!confirm("从池中移除账号「" + name + "」？将同时删除其配置文件 /etc/ajiasu-" + name + ".conf（不可恢复）。")) return;
  try {
    await adminPost("/api/admin/pool/account/remove", { name, deleteConfig: true });
    showToast("已移除 " + name);
    await loadPool();
  } catch (e) { showToast(e.message); }
}

export async function poolRelease() {
  if (!confirm("释放当前租约？若此刻 1080 被签到轮换占用，池子会拒绝而不会抢占。")) return;
  try { await adminPost("/api/admin/pool/release", {}); showToast("已释放"); await loadPool(); }
  catch (e) { showToast(e.message); }
}

export async function poolReap() {
  try { await adminPost("/api/admin/pool/reap", {}); showToast("已回收超时租约"); await loadPool(); }
  catch (e) { showToast(e.message); }
}

document.getElementById("poolBody").addEventListener("click", e => {
  const btn = e.target.closest("[data-pool-act]");
  if (!btn) return;
  const act = btn.dataset.poolAct, name = btn.dataset.name;
  if (act === "check") poolCheck(name);
  else if (act === "deepcheck") poolDeepCheck(name);
  else if (act === "edit") openAccountEdit(name);
  else if (act === "remove") poolRemoveAccount(name);
});