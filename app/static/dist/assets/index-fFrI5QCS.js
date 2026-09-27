(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),t.credentials=e.crossOrigin===`use-credentials`?`include`:e.crossOrigin===`anonymous`?`omit`:`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();var e={deviceId:localStorage.getItem(`device_id`)||``,deviceSig:localStorage.getItem(`device_sig`)||``,latestResults:[],extractingNow:!1,lastAutoValue:``,autoExtractTimer:null,coverPreviewTrigger:null,coverPreviewBodyOverflow:``,clearArmTimer:null};async function t(t,r={}){let i={...r.headers||{}};i[`Content-Type`]=`application/json`,e.deviceId&&(i[`X-Device-Id`]=e.deviceId),e.deviceSig&&(i[`X-Device-Sig`]=e.deviceSig);let a=await fetch(t,{...r,headers:i});if(a.status===429){let e=await a.json().catch(()=>({}));throw Error(e.error||`请求过于频繁`)}if(!a.ok){let e=await a.json().catch(()=>({}));throw Error(e.error||`请求失败`)}return n(a)}async function n(t){t.clone();let n=await t.json().catch(()=>({}));return n.device_id&&n.device_sig&&(e.deviceId=n.device_id,e.deviceSig=n.device_sig,localStorage.setItem(`device_id`,e.deviceId),localStorage.setItem(`device_sig`,e.deviceSig)),n}function r(t={}){let n={"Content-Type":`application/json`,...t};return e.deviceId&&(n[`X-Device-Id`]=e.deviceId),e.deviceSig&&(n[`X-Device-Sig`]=e.deviceSig),n}function i(e){return String(e??``).replace(/&/g,`&amp;`).replace(/</g,`&lt;`).replace(/>/g,`&gt;`).replace(/"/g,`&quot;`).replace(/'/g,`&#39;`)}function a(e){let t=document.createElement(`div`);return t.innerHTML=e,t.textContent}async function o(){try{s(await t(`/api/stats`))}catch{document.getElementById(`statsGrid`).innerHTML=`<div class="card" style="color:var(--error);text-align:center;">统计加载失败</div>`}}function s(e){let t=Object.entries(e.platform_dist||{}).map(([e,t])=>`<span class="platform-chip">${i(e)} ${t} 条</span>`).join(``),n=document.getElementById(`statsGrid`);n.innerHTML=`
    <div class="stat-card ok">
      <div class="num">${e.total}</div>
      <div class="label">累计提取</div>
      <div class="sub">${e.ok} 成功 · ${e.fail} 失败</div>
    </div>
    <div class="stat-card">
      <div class="num">${e.success_rate}%</div>
      <div class="label">成功率</div>
      <div class="sub">${e.total?`最近 200 条内统计`:`暂无数据`}</div>
    </div>
    <div class="card" style="grid-column: 1 / -1;">
      <h2 style="margin-bottom:4px;">平台分布</h2>
      <div class="platform-chips">${t||`<span style="color:var(--text2);font-size:13px;">暂无成功提取记录</span>`}</div>
    </div>`;let r=document.getElementById(`statsTrend`);if(!(e.trend&&e.trend.some(e=>e.count>0))){r.innerHTML=`<div style="color:var(--text2);font-size:13px;padding:20px 0;">近 7 天暂无提取记录</div>`;return}let a=Math.max(...e.trend.map(e=>e.count),1),o=e.trend.length,s=e=>34+(o===1?358:e/(o-1)*716),c=e=>136-e/a*114,l=``;for(let e=0;e<=2;e++){let t=Math.round(a/2*e),n=c(t);l+=`<line class="grid-line" x1="34" y1="${n}" x2="750" y2="${n}"/>`,l+=`<text class="axis-label" x="28" y="${n+3}" text-anchor="end">${t}</text>`}let u=`M`+e.trend.map((e,t)=>`${s(t)},${c(e.count)}`).join(` L`),d=`${u} L${s(o-1)},136 L${s(0)},136 Z`,f=e.trend.map((e,t)=>`
    <circle class="dot" cx="${s(t)}" cy="${c(e.count)}" r="4">
      <title>${i(e.date)}：${e.count} 条</title>
    </circle>
    <text class="value-label" x="${s(t)}" y="${c(e.count)-9}" text-anchor="middle">${e.count}</text>
    <text class="axis-label" x="${s(t)}" y="154" text-anchor="middle">${i(e.date)}</text>`).join(``);r.innerHTML=`
    <svg class="trend-line" viewBox="0 0 760 160" preserveAspectRatio="xMidYMid meet">
      ${l}
      <path d="${d}" fill="var(--primary)" opacity="0.08"/>
      <path d="${u}" fill="none" stroke="var(--primary)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${f}
    </svg>`}function c(e){let t=document.getElementById(`toast`);t.textContent=e,t.classList.add(`show`),clearTimeout(t._timer),t._timer=setTimeout(()=>t.classList.remove(`show`),1600)}function l(e,t,n=``){let r=n===`xiaohongshu`,a=t||`内容封面`,o=r?`红`:n===`douyin`?`抖`:`图`,s=`cover-button`+(r?` cover-xhs`:``);if(!e)return`<button class="${s}" type="button" disabled aria-label="暂无${i(a)}预览">
      <span class="cover-placeholder" aria-hidden="true">${o}</span>
    </button>`;let c=`/api/cover?url=`+encodeURIComponent(e);return`<button class="${s}" type="button" data-src="${c}" data-alt="${i(a)}" onclick="openCoverPreview(this.dataset.src, this.dataset.alt)" aria-label="预览${i(a)}">
    <span class="cover-placeholder" aria-hidden="true">${o}</span>
    <img class="cover" src="${c}" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="" onerror="this.closest('.cover-button').classList.add('image-failed'); this.remove()">
    <span class="cover-hint" aria-hidden="true">预览</span>
  </button>`}function u(e,t){let n=e.platform_raw===`douyin`?`<span class="platform-tag platform-douyin">抖音</span>`:e.platform_raw===`xiaohongshu`?`<span class="platform-tag platform-xiaohongshu">小红书</span>`:``,r=e.success?e.partial?`<span class="result-badge badge-ok">✓ 已转换</span>`:`<span class="result-badge badge-ok">✓ 成功</span>`:`<span class="result-badge badge-fail">✗ 失败</span>`,a=`
    <div class="result-head">
      <span class="result-index">#${t+1}</span>
      ${n}
      ${r}
    </div>`;if(e.success){let t=e.title||e.caption||e.platform||`内容`,n=`<div class="cover-wrap">${l(e.cover_url,t,e.platform_raw)}</div>`;a+=`
      <div class="result-main">
        ${n}
        <div class="result-content">
      <div class="field">
        <div class="field-label">转换链接（推荐复制）</div>
        <div class="field-value link" title="${i(e.canonical_url)}">${i(e.canonical_url)}</div>
      </div>
      <div class="field">
        <div class="field-label">文案</div>
        <div class="caption-wrap">
          <div class="field-value caption">${i(e.caption)||`（无文案）`}</div>
          <button class="caption-toggle" type="button" onclick="toggleCaption(this)">展开全部 ▼</button>
        </div>
      </div>
      ${e.hint?`<div class="field partial-hint"><div class="field-label">提取提示</div><div class="field-value">${i(e.hint)}</div></div>`:``}
      <div class="copy-row">
        <button class="btn btn-copy" onclick="copyText(unescapeHtml(this.parentNode.previousElementSibling.previousElementSibling.querySelector('.field-value').innerText), this)" data-kind="canonical">📋 复制链接</button>
        <button class="btn btn-copy" onclick="copyText(this.parentNode.parentNode.querySelector('.caption').innerText, this)" data-kind="caption">📋 复制文案</button>
      </div>
        </div>
      </div>`;let r=[];e.title&&e.title!==e.caption&&r.push([`标题`,e.title]),e.author_name&&r.push([`作者`,e.author_name]),e.publish_time&&r.push([`发布时间`,e.publish_time]),e.like_count&&r.push([`点赞`,String(e.like_count)]),e.video_url&&e.video_url!==e.canonical_url&&r.push([`视频链接`,e.video_url,!0]),r.length&&(a+=`<div class="field" style="margin-top:10px;"><div class="field-label">更多信息</div>`,r.forEach(([e,t,n])=>{a+=`<div class="field"><div class="field-label">${e}</div><div class="field-value${n?` link`:``}"${n?` title="${i(t)}"`:``}>${i(t)}</div></div>`}),a+=`</div>`),e.video_url&&e.video_url!==e.canonical_url&&(a+=`<div class="result-actions"><button class="btn btn-sm btn-ghost" onclick="copyText(this.closest('.result-item').dataset.videoUrl, this)">复制原视频链接</button></div>`)}else a+=`
      <div class="field">
        <div class="field-label">原始链接</div>
        <div class="field-value link" title="${i(e.original_url)}">${i(e.original_url)}</div>
      </div>
      <div class="field">
        <div class="field-label">错误信息</div>
        <div class="field-value" style="color:var(--error);">${i(e.error)}</div>
      </div>
      ${e.hint?`<div class="field"><div class="field-label">提示</div><div class="field-value" style="color:var(--text2);">${i(e.hint)}</div></div>`:``}
      <div class="copy-row" style="margin-top:12px;">
        <button class="btn btn-ghost" onclick="copyText(this.closest('.result-item').dataset.originalUrl, this)">📋 复制原链接</button>
        <button class="btn btn-ghost" onclick="retryLink(this, ${t})">🔄 重试这条</button>
      </div>`;return a}function d(e){e.querySelectorAll(`.caption-wrap`).forEach(e=>{let t=e.querySelector(`.caption`);t&&t.scrollHeight>t.clientHeight+2&&e.classList.add(`show-toggle`)})}function f(e){e.textContent=e.closest(`.caption-wrap`).querySelector(`.caption`).classList.toggle(`expanded`)?`收起 ▲`:`展开全部 ▼`}async function p(){let e=document.getElementById(`historyList`),n=document.getElementById(`historyStatus`);n.textContent=`加载中...`;try{let r=await t(`/api/history`);if(n.textContent=r.items.length?`共 ${r.items.length} 条历史记录（仅自己可见）`:`暂无历史记录`,!r.items.length){e.innerHTML=`<div class="card" style="color:var(--text2);text-align:center;">暂无历史记录</div>`;return}e.innerHTML=r.items.map((e,t)=>`
      <div class="card" style="padding:14px 16px;">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;">
          <div style="font-size:13px;color:var(--text2);">#${r.items.length-t} · ${i(e.platform||`未知平台`)} · ${i(e.created_at)}</div>
          ${e.status===`success`?`<span class="result-badge badge-ok">成功</span>`:`<span class="result-badge badge-fail">失败</span>`}
        </div>
        <div class="history-card-body">
          ${e.status===`success`&&e.cover_url?`<div class="cover-wrap">${l(e.cover_url,e.title||e.caption||e.platform||`内容`,e.platform===`小红书`?`xiaohongshu`:e.platform===`抖音`?`douyin`:``)}</div>`:``}
          <div class="history-card-content">
            ${e.status===`success`?`
            <div class="field" style="margin-top:8px;">
              <div class="field-label">转换链接</div>
              <div class="field-value link" title="${i(e.canonical_url||e.original_url)}">${i(e.canonical_url||e.original_url)}</div>
            </div>
            ${e.caption?`<div class="field"><div class="field-label">文案</div><div class="caption-wrap"><div class="field-value caption">${i(e.caption)}</div><button class="caption-toggle" type="button" onclick="toggleCaption(this)">展开全部 ▼</button></div></div>`:``}
            <div class="copy-row">
              <button class="btn btn-sm btn-copy" onclick="copyText(this.parentNode.parentNode.querySelector('.link').innerText, this)">复制链接</button>
              ${e.caption?`<button class="btn btn-sm btn-copy" onclick="copyText(this.parentNode.parentNode.querySelector('.caption').innerText, this)">复制文案</button>`:``}
            </div>`:`
            <div class="field" style="margin-top:8px;">
              <div class="field-label">原始链接</div>
              <div class="field-value link" title="${i(e.original_url)}">${i(e.original_url)}</div>
            </div>
            <div class="field"><div class="field-label">错误</div><div class="field-value" style="color:var(--error);">${i(e.error)}</div></div>`}
          </div>
        </div>
      </div>
    `).join(``),d(e)}catch{n.textContent=`加载历史失败`}}async function m(){let n=document.getElementById(`btnClearHistory`);if(!n.classList.contains(`confirming`)){n.classList.add(`confirming`),n.textContent=`⚠ 再点一次确认清空`,clearTimeout(e.clearArmTimer),e.clearArmTimer=setTimeout(()=>{n.classList.remove(`confirming`),n.textContent=`🗑 清空历史`},3e3);return}clearTimeout(e.clearArmTimer),n.classList.remove(`confirming`),n.textContent=`🗑 清空历史`;try{await t(`/api/history`,{method:`DELETE`}),c(`历史已清空`),p()}catch{c(`清空失败`)}}function h(e){document.querySelectorAll(`.tab`).forEach(t=>{let n=t.dataset.tab===e;t.classList.toggle(`active`,n),t.setAttribute(`aria-selected`,String(n)),t.tabIndex=n?0:-1}),document.getElementById(`tab-extract`).classList.toggle(`hidden`,e!==`extract`),document.getElementById(`tab-stats`).classList.toggle(`hidden`,e!==`stats`),document.getElementById(`tab-history`).classList.toggle(`hidden`,e!==`history`),e===`stats`&&o(),e===`history`&&p()}function g(e){if(![`ArrowLeft`,`ArrowRight`,`Home`,`End`].includes(e.key))return;let t=[...document.querySelectorAll(`.tab`)],n=t.indexOf(e.currentTarget),r=n;e.key===`ArrowLeft`&&(r=(n-1+t.length)%t.length),e.key===`ArrowRight`&&(r=(n+1)%t.length),e.key===`Home`&&(r=0),e.key===`End`&&(r=t.length-1),e.preventDefault(),t[r].focus(),h(t[r].dataset.tab)}function _(t,n){if(!t)return;let r=document.getElementById(`coverPreview`),i=document.getElementById(`coverPreviewImage`),a=document.getElementById(`coverPreviewTitle`);e.coverPreviewTrigger=document.activeElement,e.coverPreviewBodyOverflow=document.body.style.overflow,i.src=t,i.alt=n||`内容封面`,a.textContent=(n||`内容封面`)+`预览`,r.classList.remove(`hidden`),document.body.style.overflow=`hidden`,document.getElementById(`coverPreviewClose`).focus()}function v(){let t=document.getElementById(`coverPreview`);t.classList.contains(`hidden`)||(t.classList.add(`hidden`),document.getElementById(`coverPreviewImage`).removeAttribute(`src`),document.body.style.overflow=e.coverPreviewBodyOverflow,e.coverPreviewTrigger&&typeof e.coverPreviewTrigger.focus==`function`&&e.coverPreviewTrigger.focus(),e.coverPreviewTrigger=null)}function y(e){e.target===e.currentTarget&&v()}document.addEventListener(`keydown`,function(e){e.key===`Escape`&&v()});async function b(t=!1){let n=document.getElementById(`inputUrls`).value,i=E(n);if(!i.length){t||c(`未识别到链接，请粘贴抖音或小红书分享链接`);return}if(i.length>20){t||c(`一次最多 20 条链接，请分批提取`);return}if(e.extractingNow)return;e.extractingNow=!0;let a=document.getElementById(`btnExtract`),o=document.getElementById(`extractStatus`),s=document.getElementById(`results`),l=document.getElementById(`btnRetryFailed`);a.disabled=!0,a.innerHTML=`<span class="loading-spinner"></span>提取中...`,o.textContent=`正在提取 ${i.length} 条链接...`,o.classList.remove(`error`),s.innerHTML=``,e.latestResults=[],l.classList.add(`hidden`);let f=0,p=0;try{let t=await fetch(`/api/extract`,{method:`POST`,headers:r(),body:JSON.stringify({urls:i})});if(!t.ok){let e=await t.json().catch(()=>({}));throw Error(e.error||`请求失败`)}if(!t.body)throw Error(`浏览器不支持流式响应`);let a=t.body.getReader(),c=new TextDecoder(`utf-8`),m=``,h=0,g=``,_=``;for(;;){let{done:t,value:n}=await a.read();if(t)break;m+=c.decode(n,{stream:!0});let r=m.split(`
`);m=r.pop();for(let t of r){if(!t.trim())continue;let n;try{n=JSON.parse(t)}catch{continue}if(n.type===`start`)n.device_id&&n.device_sig&&(g=n.device_id,_=n.device_sig),o.textContent=`正在提取 ${n.total} 条链接...（完成 0/${n.total}）`;else if(n.type===`item`){let t=n.data;t.source_index=Number.isInteger(n.source_index)?n.source_index:h,e.latestResults.push(t),h++,t.success?f++:p++;let r=document.createElement(`div`);r.className=`result-item`,r.dataset.sourceIndex=String(t.source_index),r.dataset.originalUrl=t.original_url||``,r.dataset.videoUrl=t.video_url||``,r.innerHTML=u(t,h-1),s.appendChild(r),d(r),o.textContent=`正在提取...（完成 ${h}/${i.length}，成功 ${f}，失败 ${p}）`}else n.type}}g&&_&&(e.deviceId=g,e.deviceSig=_,localStorage.setItem(`device_id`,e.deviceId),localStorage.setItem(`device_sig`,e.deviceSig)),o.textContent=`完成：成功 ${f} 条，失败 ${p} 条`,l.classList.toggle(`hidden`,p===0),e.lastAutoValue=n}catch(e){o.textContent=e.message||`提取失败`,o.classList.add(`error`)}finally{e.extractingNow=!1,a.disabled=!1,a.textContent=`🔍 提取全部`}}async function x(t){let n=await fetch(`/api/extract`,{method:`POST`,headers:r(),body:JSON.stringify({urls:[t]})});if(!n.ok){let e=await n.json().catch(()=>({}));throw Error(e.error||`请求失败`)}if(!n.body)throw Error(`浏览器不支持流式响应`);let i=n.body.getReader(),a=new TextDecoder(`utf-8`),o=``,s=null;for(;;){let{done:t,value:n}=await i.read();if(t)break;o+=a.decode(n,{stream:!0});let r=o.split(`
`);o=r.pop();for(let t of r)if(t.trim())try{let n=JSON.parse(t);n.type===`item`&&(s=n.data),n.type===`start`&&n.device_id&&n.device_sig&&(e.deviceId=n.device_id,e.deviceSig=n.device_sig,localStorage.setItem(`device_id`,e.deviceId),localStorage.setItem(`device_sig`,e.deviceSig))}catch{}}if(!s)throw Error(`未获取到提取结果`);return s}async function S(e,t){let n=e.closest(`.result-item`),r=n&&n.dataset.originalUrl;if(!r){c(`找不到原始链接，请重新粘贴`);return}let i=e.textContent;e.disabled=!0,e.innerHTML=`<span class="loading-spinner"></span>重试中...`;try{let a=await x(r);if(a&&a.success){if(c(`重试成功！`),n){n.remove();let e=document.getElementById(`results`);a.source_index=Number(n.dataset.sourceIndex);let r=renderSingleResult(a,t),i=[...e.querySelectorAll(`.result-item`)],o=Math.min(t,i.length);o===0?e.prepend(r):i[o-1].after(r),w()}}else c(a&&a.error||`重试失败`),e.disabled=!1,e.textContent=i}catch(t){c(`重试失败：`+(t.message||`网络错误`)),e.disabled=!1,e.textContent=i}}async function C(){let t=e.latestResults.filter(e=>!e.success&&e.original_url);t.length&&!e.extractingNow&&(document.getElementById(`inputUrls`).value=t.map(e=>e.original_url).join(`
`),D(),c(`正在重试 ${t.length} 条失败链接`),await b())}function w(){document.getElementById(`results`).querySelectorAll(`.result-item .result-index`).forEach((e,t)=>{e.textContent=`#`+(t+1)})}function T(){document.getElementById(`inputUrls`).value=``,document.getElementById(`extractStatus`).textContent=``,document.getElementById(`results`).innerHTML=``,e.lastAutoValue=``,clearTimeout(e.autoExtractTimer),D()}function E(e){let t=(e||``).match(/https?:\/\/[^\s\u4e00-\u9fff]+/g)||[],n=new Set;return t.map(e=>e.trim().replace(/[.,;:!?，。；：！？）】》]+$/g,``)).filter(e=>e&&!n.has(e)&&n.add(e))}function D(){let e=document.getElementById(`inputUrls`).value,t=E(e).length,n=document.getElementById(`linkCount`);n.innerHTML=t===0?``:`🔗 已识别 <span class="count">${t}</span> 条链接`+(t>20?`<span style="color:var(--error);margin-left:8px;">（超过 20 条上限，请分批）</span>`:``)}function O(){D(),clearTimeout(e.autoExtractTimer);let t=document.getElementById(`inputUrls`).value;!e.extractingNow&&t.trim()&&(e.autoExtractTimer=setTimeout(()=>{t!==e.lastAutoValue&&E(t).length&&b(!0)},1e3))}async function k(e,t){try{await navigator.clipboard.writeText(e),A(t)}catch{let n=document.createElement(`textarea`);n.value=e,n.style.position=`fixed`,n.style.opacity=`0`,document.body.appendChild(n),n.select();try{document.execCommand(`copy`),A(t)}catch{c(`复制失败，请手动选择复制`)}document.body.removeChild(n)}}function A(e){let t=e.textContent;e.textContent=`✅ 已复制`,e.classList.add(`copied`),setTimeout(()=>{e.textContent=t,e.classList.remove(`copied`)},1200)}Object.assign(window,{switchTab:h,handleTabKey:g,clearInput:T,onInputChange:O,doExtract:b,retryFailedLinks:C,copyText:k,toggleCaption:f,retryLink:S,loadHistory:p,clearHistory:m,openCoverPreview:_,closeCoverPreview:v,closeCoverPreviewOnBackdrop:y,unescapeHtml:a}),`serviceWorker`in navigator&&window.isSecureContext&&window.addEventListener(`load`,function(){navigator.serviceWorker.register(`/static/sw.js`).catch(function(){})}),(function(){let e=document.getElementById(`inputUrls`);if(!e)return;let t=()=>window.matchMedia(`(max-width: 600px)`).matches;e.addEventListener(`focus`,function(){t()&&setTimeout(()=>{let t=e.getBoundingClientRect(),n=window.innerHeight;t.bottom>n*.55&&window.scrollTo({top:e.offsetTop-80,behavior:`smooth`})},300)})})(),window.addEventListener(`load`,function(){});