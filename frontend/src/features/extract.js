import { state } from "../core/state.js";
import { buildHeaders } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { extractUrls, updateLinkCount } from "./input.js";
import { copyText } from "./copy.js";
import { renderSingleResult } from "./results.js";

/* ── 骨架卡（v1.19.0）：提取开始先铺 N 张占位，流式返回逐张替换 ── */
function skeletonHtml(i) {
  return `
    <div class="result-head"><span class="result-index">#${i + 1}</span><span class="skel skel-tag"></span></div>
    <div class="result-main">
      <div class="skel skel-cover"></div>
      <div class="result-content">
        <div class="skel skel-line w70"></div>
        <div class="skel skel-line w90"></div>
        <div class="skel skel-line w45"></div>
      </div>
    </div>`;
}

function showSkeletons(box, count) {
  box.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const s = document.createElement("div");
    s.className = "result-item skeleton";
    s.innerHTML = skeletonHtml(i);
    box.appendChild(s);
  }
}

/* ── 进度条（v1.19.0）：蓝段=成功、红段=失败，宽度按 urls 总数推进 ── */
function resetProgress() {
  clearTimeout(state.progressHideTimer);
  const bar = document.getElementById("extractProgress");
  if (!bar) return;
  bar.classList.remove("hidden");
  document.getElementById("progressOk").style.width = "0%";
  document.getElementById("progressFail").style.width = "0%";
}

function updateProgress(okCount, failCount, total) {
  const ok = document.getElementById("progressOk");
  const fail = document.getElementById("progressFail");
  if (!ok || !fail || !total) return;
  ok.style.width = (okCount / total * 100) + "%";
  fail.style.width = (failCount / total * 100) + "%";
}

function hideProgress() {
  const bar = document.getElementById("extractProgress");
  if (bar) bar.classList.add("hidden");
}

/* ── 批量复制条（v1.19.0） ─────────────────────────────────────── */
function showCopyAllBar(okCount) {
  const bar = document.getElementById("copyAllBar");
  const hint = document.getElementById("copyAllHint");
  if (!bar) return;
  bar.classList.toggle("hidden", okCount === 0);
  if (hint) hint.textContent = okCount ? `${okCount} 条成功结果可一键复制` : "";
}

export async function copyAllLinks(btn) {
  const items = state.latestResults.filter(r => r.success && r.canonical_url);
  if (!items.length) { showToast("没有可复制的成功链接"); return; }
  await copyText(items.map(r => r.canonical_url).join("\n"), btn);
}

export async function copyAllCaptions(btn) {
  const items = state.latestResults.filter(r => r.success && r.caption);
  if (!items.length) { showToast("没有可复制的成功文案"); return; }
  await copyText(items.map(r => r.caption).join("\n"), btn);
}

export async function doExtract(isAuto = false) {
  const raw = document.getElementById("inputUrls").value;
  const urls = extractUrls(raw);
  if (!urls.length) {
    if (!isAuto) showToast("未识别到链接，请粘贴抖音、小红书或视频号的分享链接");
    return;
  }
  if (urls.length > 20) {
    if (!isAuto) showToast("一次最多 20 条链接，请分批提取");
    return;
  }
  if (state.extractingNow) return;
  state.extractingNow = true;

  const btn = document.getElementById("btnExtract");
  const status = document.getElementById("extractStatus");
  const box = document.getElementById("results");
  const retryFailedBtn = document.getElementById("btnRetryFailed");
  btn.disabled = true;
  btn.innerHTML = '<span class="loading-spinner"></span>提取中...';
  status.textContent = `正在提取 ${urls.length} 条链接...`;
  status.classList.remove("error");
  showSkeletons(box, urls.length);  // 清空旧结果，先铺骨架卡
  state.latestResults = [];
  retryFailedBtn.classList.add("hidden");
  showCopyAllBar(0);
  resetProgress();
  const skels = [...box.children];

  let okCount = 0;
  let failCount = 0;
  let dupCount = 0;

  try {
    let res = await fetch("/api/extract", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify({ urls }),
    });
    // 请求失败：直接抛出错误信息
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || "请求失败");
    }
    if (!res.body) throw new Error("浏览器不支持流式响应");

    // 流式读取 ndjson：每条完成即时渲染
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let appended = 0;
    let deviceIdFromServer = "";
    let deviceSigFromServer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();  // 最后一段可能不完整，留到下次
      for (const line of lines) {
        if (!line.trim()) continue;
        let msg;
        try { msg = JSON.parse(line); } catch (e) { continue; }
        if (msg.type === "start") {
          if (msg.device_id && msg.device_sig) {
            deviceIdFromServer = msg.device_id;
            deviceSigFromServer = msg.device_sig;
          }
          status.textContent = `正在提取 ${msg.total} 条链接...（完成 ${0}/${msg.total}）`;
        } else if (msg.type === "item") {
          const r = msg.data;
          r.source_index = Number.isInteger(msg.source_index) ? msg.source_index : appended;
          state.latestResults.push(r);
          appended++;
          if (r.success) okCount++; else failCount++;
          if (r.success && r.duplicate_this_month) dupCount++;
          // 骨架卡按到达顺序逐张替换成真卡（renderSingleResult 内含平台 class 与文案折叠初始化）
          const item = renderSingleResult(r, appended - 1);
          const skel = skels[appended - 1];
          if (skel) skel.replaceWith(item); else box.appendChild(item);
          updateProgress(okCount, failCount, urls.length);
          status.textContent = `正在提取...（完成 ${appended}/${urls.length}，成功 ${okCount}，失败 ${failCount}）`;
        } else if (msg.type === "end") {
          // 结束帧：有重复提报时统一提醒一次（卡片上另有逐条标记）
          if (dupCount > 0) {
            showToast(`本月已提报过 ${dupCount} 条，重复提交不增加有效条数`);
          }
        }
      }
    }

    if (deviceIdFromServer && deviceSigFromServer) {
      state.deviceId = deviceIdFromServer;
      state.deviceSig = deviceSigFromServer;
      localStorage.setItem("device_id", state.deviceId);
      localStorage.setItem("device_sig", state.deviceSig);
    }
    status.textContent = `完成：成功 ${okCount} 条，失败 ${failCount} 条`;
    retryFailedBtn.classList.toggle("hidden", failCount === 0);
    showCopyAllBar(okCount);
    state.lastAutoValue = raw;  // 记录已提取内容，避免自动重复提取
  } catch (e) {
    status.textContent = e.message || "提取失败";
    status.classList.add("error");
    // 移除还没被替换的骨架卡，避免报错后占位卡永远挂着
    box.querySelectorAll(".result-item.skeleton").forEach(el => el.remove());
    reindexResults();
  } finally {
    state.extractingNow = false;
    btn.disabled = false;
    btn.textContent = "🔍 提取全部";
    // 进度条停留 2.5s 展示最终分布（蓝=成功/红=失败），然后收起
    clearTimeout(state.progressHideTimer);
    state.progressHideTimer = setTimeout(hideProgress, 2500);
  }
}

/** 流式提取单条链接，返回结果对象（供重试用） */
export async function extractOneStream(url) {
  let res = await fetch("/api/extract", {
    method: "POST",
    headers: buildHeaders(),
    body: JSON.stringify({ urls: [url] }),
  });
  // 请求失败：直接抛出错误信息
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.error || "请求失败");
  }
  if (!res.body) throw new Error("浏览器不支持流式响应");
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let result = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.type === "item") result = msg.data;
        if (msg.type === "start" && msg.device_id && msg.device_sig) {
          state.deviceId = msg.device_id;
          state.deviceSig = msg.device_sig;
          localStorage.setItem("device_id", state.deviceId);
          localStorage.setItem("device_sig", state.deviceSig);
        }
      } catch (e) { /* 忽略坏行 */ }
    }
  }
  if (!result) throw new Error("未获取到提取结果");
  return result;
}

/** 读取剪贴板并直接提取（v1.14.0）。
 *  ⚠️ clipboard.readText 仅在 HTTPS/localhost 可用；本工具目前是 HTTP，
 *  大多数环境下会走 catch 分支提示用户手动粘贴。上了 HTTPS 后自动生效。 */
export async function readClipboardExtract() {
  const ta = document.getElementById("inputUrls");
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.readText !== "function") {
      throw new Error("unsupported");
    }
    const text = await navigator.clipboard.readText();
    const urls = extractUrls(text || "");
    if (!urls.length) {
      showToast("剪贴板里没有识别到链接");
      return;
    }
    ta.value = text.trim();
    updateLinkCount();
    await doExtract();
  } catch (e) {
    showToast("无法自动读取剪贴板（需要 HTTPS 环境），请长按输入框粘贴");
  }
}

/** 重试单条失败链接：重新提取并替换该卡片 */
export async function retryLink(btn, index) {
  const card = btn.closest(".result-item");
  const target = card && card.dataset.originalUrl;
  if (!target) { showToast("找不到原始链接，请重新粘贴"); return; }

  const oldText = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="loading-spinner"></span>重试中...';
  try {
    const r = await extractOneStream(target);
    if (r && r.success) {
      showToast("重试成功！");
      // 用新结果替换整个卡片
      if (card) {
        card.remove();
        const box = document.getElementById("results");
        r.source_index = Number(card.dataset.sourceIndex);
        const rebuilt = renderSingleResult(r, index);
        // 重新排序索引
        const all = [...box.querySelectorAll(".result-item")];
        const pos = Math.min(index, all.length);
        if (pos === 0) box.prepend(rebuilt);
        else all[pos - 1].after(rebuilt);
        reindexResults();
      }
    } else {
      showToast((r && r.error) || "重试失败");
      btn.disabled = false;
      btn.textContent = oldText;
    }
  } catch (e) {
    showToast("重试失败：" + (e.message || "网络错误"));
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

export async function retryFailedLinks() {
  const failed = state.latestResults.filter(r => !r.success && r.original_url);
  if (!failed.length || state.extractingNow) return;
  document.getElementById("inputUrls").value = failed.map(r => r.original_url).join("\n");
  updateLinkCount();
  showToast(`正在重试 ${failed.length} 条失败链接`);
  await doExtract();
}

/** 重新编号所有结果卡片索引 */
export function reindexResults() {
  const box = document.getElementById("results");
  box.querySelectorAll(".result-item .result-index").forEach((el, i) => {
    el.textContent = "#" + (i + 1);
  });
}