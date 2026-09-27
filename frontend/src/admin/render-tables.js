import { esc, fmt } from "./core.js";
import { state, expanded } from "./state.js";

export function renderDevices(devices) {
  const body = document.getElementById("deviceBody");
  const empty = document.getElementById("deviceEmpty");
  if (!devices || !devices.length) { body.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const query = (document.getElementById("deviceSearch").value || "").trim().toLowerCase();
  const sort = document.getElementById("deviceSort").value;
  const filtered = devices.filter(d => !query || String(d.device_id || "").toLowerCase().includes(query));
  filtered.sort((a, b) => sort === "fail" ? b.fail - a.fail : sort === "rate" ? b.success_rate - a.success_rate : sort === "recent" ? String(b.last_at).localeCompare(String(a.last_at)) : b.total - a.total);
  const visible = expanded.devices ? filtered : filtered.slice(0, 15);
  body.innerHTML = visible.map(d => `
    <tr>
      <td style="font-family:monospace;font-size:12px;">${esc(d.device_id)}</td>
      <td class="num">${fmt(d.total)}</td>
      <td class="num ok">${fmt(d.ok)}</td>
      <td class="num fail">${fmt(d.fail)}</td>
      <td class="num ${d.success_rate >= 90 ? "ok" : d.success_rate < 70 ? "fail" : ""}">${d.success_rate}%</td>
      <td style="color:var(--text2);font-size:12px;">${esc(d.last_at || "-")}</td>
    </tr>`).join("");
  const more = document.getElementById("deviceMore");
  more.classList.toggle("hidden", filtered.length <= 15);
  more.textContent = expanded.devices ? "收起设备列表" : `展开全部设备（${filtered.length}）`;
}

// 细粒度归因的中文说明：看板不再只显示「平台暂时限制」这句万能文案，
// 而是直接摊开真实根因（限流 / 内容没了 / 页面结构变了 / 用户输入问题）。
const KIND_LABELS = {
  platform_limited: "平台限流（换IP可解）", short_link_blocked: "短链被限流",
  not_found: "内容不存在", expired_content: "作品已删除/不可见", note_missing: "笔记不存在",
  page_changed: "页面结构变化", upstream_data_missing: "平台未返回数据",
  upstream_http: "上游HTTP错误", network: "网络异常", redirect_blocked: "重定向被拦",
  invalid_input: "用户输入问题", unsupported_domain: "不支持的域名",
  url_truncated: "链接被截断", missing_xsec_token: "缺 xsec_token", malformed_url: "链接粘贴变形",
  internal_error: "未预期异常", "(未归因·历史数据)": "未归因（历史数据）",
};

export function kindLabel(kind) { return KIND_LABELS[kind] || kind; }

export function renderErrors(errors, kinds) {
  const kindEl = document.getElementById("errorKindSummary");
  if (kindEl) {
    kindEl.innerHTML = (kinds || []).map(k =>
      `<span class="kind-chip">${esc(kindLabel(k.kind))} <b>${fmt(k.count)}</b></span>`
    ).join("");
  }
  const el = document.getElementById("errorList");
  if (!errors || !errors.length) { el.innerHTML = '<div class="empty">暂无失败记录</div>'; return; }
  const total = errors.reduce((n,e) => n + Number(e.count || 0), 0);
  const visible = expanded.errors ? errors : errors.slice(0, 10);
  el.classList.toggle("list-expanded", expanded.errors);
  document.getElementById("errorSummary").textContent = `${fmt(total)} 次失败`;
  el.innerHTML = visible.map((e,i) => { const pct = total ? (Number(e.count)/total*100).toFixed(1) : 0; const action = errorAction(e.error, e.error_kind); return `<div class="err-rank"><span class="rank">${String(i+1).padStart(2,"0")}</span><span class="txt" title="${esc(e.error)}">${esc(e.error)}${action ? `<span class="action-tag">${esc(action)}</span>` : ""}<span class="err-bar"><i style="width:${pct}%"></i></span></span><span class="count">${fmt(e.count)}<small style="color:var(--text2);font-weight:400"> · ${pct}%</small></span></div>`; }).join("");
  const more = document.getElementById("errorMore"); more.classList.toggle("hidden", errors.length <= 10); more.textContent = expanded.errors ? "收起错误列表" : `展开全部错误（${errors.length}）`;
}
// 处理建议按 error_kind 判定（后端已归因），**不以报错文案为依据** ——
// v1.7.4/v1.7.5 改过用户可见文案后，原先的文案匹配（xsec_token / 作品 ID / 风控 / 不支持）
// 全部失配，输入类错误被一律导到「查看服务日志」，给运营的指导是错的。
// 下面的文案匹配只作兜底，用于 error_kind 上线前的历史行。
const ACTION_BY_KIND = {
  platform_limited: "稍后重试 / 检查平台风控",
  short_link_blocked: "稍后重试 / 检查平台风控",
  expired_content: "作品已删除或不可见，重试无用",
  not_found: "确认作品是否仍存在",
  note_missing: "确认作品是否仍存在",
  page_changed: "平台页面结构可能变了，查日志确认",
  upstream_data_missing: "平台未返回数据，查日志确认",
  upstream_error: "查服务日志",
  upstream_http: "查服务日志",
  redirect_blocked: "链接地址不受支持",
  network: "网络异常，稍后重试",
  invalid_input: "确认是否为抖音或小红书链接",
  unsupported_domain: "确认是否为抖音或小红书链接",
  url_truncated: "让用户重新复制完整链接",
  missing_xsec_token: "让用户重新复制分享链接",
  malformed_url: "让用户重新复制链接（疑似粘贴变形）",
  internal_error: "查看服务日志",
};

export function errorAction(error, kind) {
  if (kind && ACTION_BY_KIND[kind]) return ACTION_BY_KIND[kind];
  const text = String(error || "");
  if (text.includes("xsec_token")) return "让用户重新复制分享链接";
  if (text.includes("截断")) return "让用户重新复制完整链接";
  if (text.includes("不支持")) return "确认是否为抖音或小红书链接";
  if (text.includes("已删除") || text.includes("设为私密")) return "作品已删除或不可见，重试无用";
  return "查看服务日志";
}

export function renderRecent(items) {
  const el = document.getElementById("recentList");
  if (!items || !items.length) { el.innerHTML = '<div class="empty">暂无动态</div>'; return; }
  const nameMap = { douyin: "抖音", xiaohongshu: "小红书" };
  const platform = document.getElementById("recentPlatform").value;
  const status = document.getElementById("recentStatus").value;
  const filtered = items.filter(it => (platform === "all" || it.platform === platform) && (status === "all" || it.status === status));
  const visible = expanded.recent ? filtered : filtered.slice(0, 15);
  el.classList.toggle("list-expanded", expanded.recent);
  if (!filtered.length) {
    el.innerHTML = '<div class="empty">当前筛选暂无动态</div>';
    document.getElementById("recentMore").classList.add("hidden");
    return;
  }
  el.innerHTML = visible.map(it => {
    const status = it.status === "success"
      ? '<span class="ok">成功</span>'
      : `<span class="fail">失败</span>`;
    const extra = it.status !== "success" && it.error ? ` · ${esc(it.error.slice(0, 30))}` : "";
    return `<div class="dyn-item">
      <span>${esc(nameMap[it.platform] || it.platform || "未知")} ${status}${extra}</span>
      <span class="dyn-time">${esc((it.created_at || "").slice(5))}</span>
    </div>`;
  }).join("");
  const more = document.getElementById("recentMore");
  more.classList.toggle("hidden", filtered.length <= 15);
  more.textContent = expanded.recent ? "收起动态列表" : `展开全部动态（${filtered.length}）`;
}

/* v1.7.8：这 7 个交互点原先各自触发一次 loadAll（7 请求 + 1 子进程），
   现改为复用缓存本地重渲染 —— renderDevices / renderRecent / renderErrors
   内部本就会自己读搜索框与下拉框的值，所以行为与改前完全一致，只是不发请求。 */
document.getElementById("deviceMore").onclick = () => { expanded.devices = !expanded.devices; renderDevices(state.deviceCache); };
document.getElementById("errorMore").onclick = () => { expanded.errors = !expanded.errors; renderErrors(state.lastErrors, state.lastErrorKinds); };
document.getElementById("recentMore").onclick = () => { expanded.recent = !expanded.recent; renderRecent(state.recentCache); };
document.getElementById("deviceSearch").oninput = (() => {
  let t = 0;
  return () => { clearTimeout(t); t = setTimeout(() => renderDevices(state.deviceCache), 120); };
})();
document.getElementById("deviceSort").onchange = () => renderDevices(state.deviceCache);
document.getElementById("recentPlatform").onchange = () => renderRecent(state.recentCache);
document.getElementById("recentStatus").onchange = () => renderRecent(state.recentCache);