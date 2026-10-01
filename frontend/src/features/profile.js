import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { loadStats } from "./stats.js";
import { fetchMonthlySummary, monthlySummaryHtml } from "./summary.js";

// ---------------------------------------------------------------- 成员署名

/**
 * 姓名 = 身份（v1.14.0，无密码）。首次访问弹引导框（含本月有效条数展示），
 * 已署名则不弹；姓名可随时改。
 *
 * v1.16.0：引导框与月度数字合并为一次性展示。此前月度提示条（z-index 1200）
 * 与全屏遮罩弹窗（z-index 1000）同帧弹出、互相遮挡，被反馈「界面很奇怪」。
 */

export function initProfile() {
  if (state.memberName) return; // 已署名，不打扰
  openNameModal();
}

export async function openNameModal() {
  const modal = document.getElementById("nameModal");
  const input = document.getElementById("memberNameInput");
  if (!modal || !input) return;
  input.value = state.memberName || "";
  // 月度数字异步填充（拉取失败就留空，不影响填名字）
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
  try {
    await apiFetch("/api/profile", { method: "POST", body: JSON.stringify({ name }) });
    state.memberName = name;
    localStorage.setItem("member_name", name);
    closeNameModal();
    showToast(`已署名：${name}`);
    // 统计页若正在显示，立即刷新姓名显示（loadStats 内部会重拉 profile）
    if (!document.getElementById("tab-stats").classList.contains("hidden")) {
      loadStats().catch(() => {});
    }
  } catch (e) {
    showToast(e.message || "保存失败，请稍后再试");
  }
}
