import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { loadStats } from "./stats.js";
import { fetchMonthlySummary, monthlySummaryHtml } from "./summary.js";

// ---------------------------------------------------------------- 成员署名

/**
 * 姓名 = 身份（v1.14.0，无密码）。首次访问弹引导框（含本月有效条数展示），
 * 已填姓名+身份则不弹；两者都可随时改。
 *
 * v1.16.0：引导框与月度数字合并为一次性展示（此前月度条与姓名弹窗同帧弹出、
   层叠互相遮挡）。v1.17.0：增加身份选择（签约创作者/创作大使/学校/区域创作者）。
 */

const IDENTITY_LABELS = {
  signed: "签约创作者",
  ambassador: "创作大使",
  school: "学校/区域创作者",
};

export function initProfile() {
  // 是否已填全以**服务端**为准：localStorage 可能只存了姓名（v1.16.1 时代
  // 还没有身份），只看本地会误判“齐全”而永远不弹 v1.17.0 的身份选择。
  apiFetch("/api/profile")
    .then((profile) => {
      const name = (profile && profile.name) || "";
      const identity = (profile && profile.identity) || "";
      if (name) state.memberName = name;          // 服务端有就用服务端的
      if (identity) state.memberIdentity = identity;
      if (name && identity) return;               // 齐全，不打扰
      openNameModal();
    })
    .catch(() => {
      // 拉取失败（网络/限流）：退回本地判断，至少别把正常人卡在弹窗前
      if (state.memberName && state.memberIdentity) return;
      openNameModal();
    });
}

export async function openNameModal() {
  const modal = document.getElementById("nameModal");
  const input = document.getElementById("memberNameInput");
  if (!modal || !input) return;
  // **先显示再拉数据**：此前 await 在弹窗之前，弱网/限速时会观感「点了没反应」。
  let name = state.memberName || "";
  let identity = state.memberIdentity || "";
  input.value = name;
  paintIdentityChoices(identity);
  setTitle(name ? "完善你的信息" : "先设置你的姓名");
  modal.classList.remove("hidden");
  setTimeout(() => input.focus(), 50);
  // 姓名与身份以服务端口径为准：换设备后本地为空、或本地只存了旧字段时，
  // 服务端是唯一权威源（initProfile 已先拉过一次，这里兜底再拉一次）
  if (!name || !identity) {
    try {
      const profile = await apiFetch("/api/profile");
      if (!name) name = (profile && profile.name) || "";
      if (!identity) identity = (profile && profile.identity) || "";
      state.memberName = name;
      state.memberIdentity = identity;
      // 拉到了就回填（不关弹窗，用户继续编辑）
      input.value = name;
      paintIdentityChoices(identity);
      setTitle(name ? "完善你的信息" : "先设置你的姓名");
    } catch (e) { /* 拉取失败保留本地值，静默 */ }
  }
  // 月度数字异步填充（拉取失败就留空，不影响填信息）
  const slot = document.getElementById("nameMonthlySlot");
  if (slot) {
    slot.innerHTML = "";
    fetchMonthlySummary().then((data) => {
      slot.innerHTML = monthlySummaryHtml(data);
    }).catch(() => {});
  }
}

/** 标题节点缺失时静默降级：不能因为一个标题让整个弹窗打不开
 *  （v1.17.3：HTML 漏写 id 导致 getElementById 返回 null 抛 TypeError，
 *   弹窗在 classList.remove("hidden") 之前就挂了，表现=按钮点了没反应）。 */
function setTitle(text) {
  const el = document.getElementById("nameModalTitle");
  if (el) el.textContent = text;
}

function paintIdentityChoices(identity) {
  document.querySelectorAll("#identityChoices .identity-btn").forEach((btn) => {
    const active = btn.dataset.identity === identity;
    btn.classList.toggle("selected", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

/** 点选身份（不落盘，保存时一起提交） */
export function selectIdentity(identity) {
  state.memberIdentity = identity;
  paintIdentityChoices(identity);
}

export function closeNameModal() {
  document.getElementById("nameModal")?.classList.add("hidden");
}

export function skipMemberName() {
  closeNameModal();
  showToast("未设置姓名，可随时在统计页补填");
}

export async function saveMemberName() {
  const input = document.getElementById("memberNameInput");
  const name = (input?.value || "").trim();
  if (!name) {
    showToast("请输入姓名");
    return;
  }
  if (name.length > 20) {
    showToast("姓名不能超过 20 个字");
    return;
  }
  if (!state.memberIdentity) {
    showToast("请选择身份");
    return;
  }
  try {
    await apiFetch("/api/profile", {
      method: "POST",
      body: JSON.stringify({ name, identity: state.memberIdentity }),
    });
    state.memberName = name;
    localStorage.setItem("member_name", name);
    localStorage.setItem("member_identity", state.memberIdentity);
    closeNameModal();
    showToast(`已保存：${name} · ${IDENTITY_LABELS[state.memberIdentity] || ""}`);
    // 统计页若正在显示，立即刷新姓名/身份显示（loadStats 内部会重拉 profile）
    if (!document.getElementById("tab-stats").classList.contains("hidden")) {
      loadStats().catch(() => {});
    }
  } catch (e) {
    showToast(e.message || "保存失败，请稍后再试");
  }
}
