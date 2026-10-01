import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { loadStats } from "./stats.js";

// ---------------------------------------------------------------- 成员署名

/**
 * 姓名 = 身份（v1.14.0，无密码）。首次访问弹登记框；后台按姓名聚合产量。
 * 名字只存 localStorage + 服务端 members 表，可随时改。
 */

export function initProfile() {
  if (state.memberName) return; // 已署名，不打扰
  openNameModal();
}

export function openNameModal() {
  const modal = document.getElementById("nameModal");
  const input = document.getElementById("memberNameInput");
  if (!modal || !input) return;
  input.value = state.memberName || "";
  modal.classList.remove("hidden");
  setTimeout(() => input.focus(), 50);
}

export function closeNameModal() {
  document.getElementById("nameModal")?.classList.add("hidden");
}

export function skipMemberName() {
  closeNameModal();
  showToast("未署名：后台将显示为「未署名」，随时可在统计页设置");
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
