import { esc } from "../core/dom.js";

export function coverPreviewHtml(coverUrl, alt, platformRaw = "") {
  const isXhs = platformRaw === "xiaohongshu";
  const label = alt || "内容封面";
  const platformMark = isXhs ? "红" : platformRaw === "douyin" ? "抖" : "图";
  const buttonClass = "cover-button" + (isXhs ? " cover-xhs" : "");
  if (!coverUrl) {
    return `<button class="${buttonClass}" type="button" disabled aria-label="暂无${esc(label)}预览">
      <span class="cover-placeholder" aria-hidden="true">${platformMark}</span>
    </button>`;
  }
  const proxyUrl = "/api/cover?url=" + encodeURIComponent(coverUrl);
  return `<button class="${buttonClass}" type="button" data-src="${proxyUrl}" data-alt="${esc(label)}" onclick="openCoverPreview(this.dataset.src, this.dataset.alt)" aria-label="预览${esc(label)}">
    <span class="cover-placeholder" aria-hidden="true">${platformMark}</span>
    <img class="cover" src="${proxyUrl}" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="" onerror="this.closest('.cover-button').classList.add('image-failed'); this.remove()">
    <span class="cover-hint" aria-hidden="true">预览</span>
  </button>`;
}

/** 生成单条结果卡片 HTML（renderResults 与重试替换共用） */
export function resultCardHtml(r, i) {
  const platformTag = r.platform_raw === "douyin"
    ? '<span class="platform-tag platform-douyin">抖音</span>'
    : r.platform_raw === "xiaohongshu"
      ? '<span class="platform-tag platform-xiaohongshu">小红书</span>'
      : "";

  const badge = r.success
    ? (r.partial
      ? '<span class="result-badge badge-ok">✓ 已转换</span>'
      : '<span class="result-badge badge-ok">✓ 成功</span>')
    : '<span class="result-badge badge-fail">✗ 失败</span>';

  let html = `
    <div class="result-head">
      <span class="result-index">#${i + 1}</span>
      ${platformTag}
      ${badge}
    </div>`;

  if (r.success) {
    // 优先展示：封面 + 转换链接 + 文案
    const coverLabel = r.title || r.caption || r.platform || "内容";
    const cover = `<div class="cover-wrap">${coverPreviewHtml(r.cover_url, coverLabel, r.platform_raw)}</div>`;
    html += `
      <div class="result-main">
        ${cover}
        <div class="result-content">
      <div class="field">
        <div class="field-label">转换链接（推荐复制）</div>
        <div class="field-value link" title="${esc(r.canonical_url)}">${esc(r.canonical_url)}</div>
      </div>
      <div class="field">
        <div class="field-label">文案</div>
        <div class="caption-wrap">
          <div class="field-value caption">${esc(r.caption) || "（无文案）"}</div>
          <button class="caption-toggle" type="button" onclick="toggleCaption(this)">展开全部 ▼</button>
        </div>
      </div>
      ${r.hint ? `<div class="field partial-hint"><div class="field-label">提取提示</div><div class="field-value">${esc(r.hint)}</div></div>` : ""}
      <div class="copy-row">
        <button class="btn btn-copy" onclick="copyText(unescapeHtml(this.parentNode.previousElementSibling.previousElementSibling.querySelector('.field-value').innerText), this)" data-kind="canonical">📋 复制链接</button>
        <button class="btn btn-copy" onclick="copyText(this.parentNode.parentNode.querySelector('.caption').innerText, this)" data-kind="caption">📋 复制文案</button>
      </div>
        </div>
      </div>`;

    // 附加信息
    const extra = [];
    if (r.title && r.title !== r.caption) extra.push(["标题", r.title]);
    if (r.author_name) extra.push(["作者", r.author_name]);
    if (r.publish_time) extra.push(["发布时间", r.publish_time]);
    if (r.like_count) extra.push(["点赞", String(r.like_count)]);
    // 原视频链接很长（带签名参数），标记为可折叠长链接（单行省略 + 悬停完整）
    if (r.video_url && r.video_url !== r.canonical_url) extra.push(["视频链接", r.video_url, true]);

    if (extra.length) {
      html += `<div class="field" style="margin-top:10px;"><div class="field-label">更多信息</div>`;
      extra.forEach(([label, value, isLongLink]) => {
        html += `<div class="field"><div class="field-label">${label}</div><div class="field-value${isLongLink ? " link" : ""}"${isLongLink ? ` title="${esc(value)}"` : ""}>${esc(value)}</div></div>`;
      });
      html += `</div>`;
    }
    if (r.video_url && r.video_url !== r.canonical_url) {
      html += `<div class="result-actions"><button class="btn btn-sm btn-ghost" onclick="copyText(this.closest('.result-item').dataset.videoUrl, this)">复制原视频链接</button></div>`;
    }
  } else {
    html += `
      <div class="field">
        <div class="field-label">原始链接</div>
        <div class="field-value link" title="${esc(r.original_url)}">${esc(r.original_url)}</div>
      </div>
      <div class="field">
        <div class="field-label">错误信息</div>
        <div class="field-value" style="color:var(--error);">${esc(r.error)}</div>
      </div>
      ${r.hint ? `<div class="field"><div class="field-label">提示</div><div class="field-value" style="color:var(--text2);">${esc(r.hint)}</div></div>` : ""}
      <div class="copy-row" style="margin-top:12px;">
        <button class="btn btn-ghost" onclick="copyText(this.closest('.result-item').dataset.originalUrl, this)">📋 复制原链接</button>
        <button class="btn btn-ghost" onclick="retryLink(this, ${i})">🔄 重试这条</button>
      </div>`;
  }
  return html;
}

/** 初始化容器内所有文案折叠：内容超过 3 行时显示「展开全部」按钮 */
export function initCaptionToggles(container) {
  container.querySelectorAll(".caption-wrap").forEach((wrap) => {
    const cap = wrap.querySelector(".caption");
    if (cap && cap.scrollHeight > cap.clientHeight + 2) {
      wrap.classList.add("show-toggle");
    }
  });
}

/** 展开/收起文案 */
export function toggleCaption(btn) {
  const wrap = btn.closest(".caption-wrap");
  const cap = wrap.querySelector(".caption");
  const expanded = cap.classList.toggle("expanded");
  btn.textContent = expanded ? "收起 ▲" : "展开全部 ▼";
}

export function renderResults(results) {
  const box = document.getElementById("results");
  box.innerHTML = "";

  results.forEach((r, i) => {
    const item = document.createElement("div");
    item.className = "result-item";
    item.innerHTML = resultCardHtml(r, i);
    box.appendChild(item);
  });
  initCaptionToggles(box);
}

/** 渲染单条结果卡片（供重试替换使用） */
export function renderSingleResult(r, index) {
  const item = document.createElement("div");
  item.className = "result-item";
  item.dataset.sourceIndex = String(Number.isInteger(r.source_index) ? r.source_index : index);
  item.dataset.originalUrl = r.original_url || "";
  item.dataset.videoUrl = r.video_url || "";
  item.innerHTML = resultCardHtml(r, index);
  initCaptionToggles(item);
  return item;
}