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
  if (state.memberName && state.memberIdentity) return; // 信息齐全，不打扰
  openNameModal();
}

export async function openNameModal() {
  const modal = document.getElementById("nameModal");
  const input = document.getElementById("memberNameInput");
  if (!modal || !input) return;
  input.value = state.memberName || "";
  // 已填过姓名时，标题换成「完善信息」而不是「先设置」
  document.getElementById("nameModalTitle").textContent =
    state.memberName ? "完善你的信息" : "先设置你的姓名";
  // 身份回显：服务端口径优先（换设备后本地为空时）
  let identity = state.memberIdentity || "";
  if (!identity) {
    try {
      const profile = await apiFetch("/api/profile");
      identity = (profile && profile.identity) || "";
    } catch (e) { /* 拉取失败就用本地值 */ }
  }
  state.memberIdentity = identity;
  paintIdentityChoices(identity);
  // 月度数字异步填充（拉取失败就留空，不影响填信息）
  const slot = document.getElementById("nameMonthlySlot");
  if (slot) {
    slot.innerHTML = "";
    fetchMonthlySummary().then((data) => {
      slot.innerHTML = monthlySummaryHtml(data);
    }).catch(() => {});
  }
  modal.classList.remove("hidden");
  setTimeout(() => input.focus(), 50);
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
