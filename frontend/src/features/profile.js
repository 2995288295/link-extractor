import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { loadStats } from "./stats.js";
import { fetchMonthlySummary, monthlySummaryHtml } from "./summary.js";

// ---------------------------------------------------------------- 成员账号（v1.18.0 登录体系）

/**
 * 姓名即身份 + **同步码（选填）**（v1.18.1，老大定：码不叫 PIN、非必填，
 * 本质是「多设备同步」而不是密码系统）。
 *
 * 与 v1.14–1.17「纯署名」的区别：进入后服务端以 member_key 作为设备标识签发
 * （HMAC），历史/统计/去重全部按人隔离——多设备填同一姓名即合并，
 * 老数据经一次性迁移继续可见。规则见后端 api_session 文档字符串。
 *
 * 防再犯（v1.17.3 的教训）：所有 DOM 操作都在「弹窗显示」之后，且节点访问
 * 一律走带存在性检查的帮手函数。
 */

const IDENTITY_LABELS = {
  signed: "签约创作者",
  ambassador: "创作大使",
  school: "学校/区域创作者",
};

function el(id) { return document.getElementById(id); }
function setText(id, text) { const n = el(id); if (n) n.textContent = text; }

/** 页面加载时判定进入状态：已进入不打扰；未进入弹引导框 */
export function initProfile() {
  apiFetch("/api/profile")
    .then((profile) => {
      if (profile && profile.name) {
        applySession(profile);          // 已进入
        return;
      }
      // 未登录：本设备历史归属过谁就预填谁（迁移后的老设备）
      openLoginModal((profile && profile.hint_name) || state.memberName || "");
    })
    .catch(() => {
      // 拉取失败不堵成员：退回本地判断
      if (state.memberName && state.memberIdentity) return;
      openLoginModal(state.memberName || "");
    });
}

/** 打开登录/注册框。prefillName 非空 = 老成员回来，文案切「登录」口径 */
export function openLoginModal(prefillName) {
  const modal = el("nameModal");
  const input = el("memberNameInput");
  const code = el("memberPinInput");
  if (!modal || !input) return;
  const returning = Boolean(prefillName);
  const loggedIn = Boolean(state.memberName);  // 已登录打开 = 修改信息/改码
  input.value = prefillName || "";
  if (code) {
    code.value = "";
    code.placeholder = loggedIn ? "新同步码（不改则留空）" : "选填，如 1234";
  }
  setText("nameModalTitle", loggedIn ? "修改信息" : returning ? "登录" : "先设置你的姓名");
  setText("nameModalDesc", loggedIn
    ? "可修改身份，或设置新的同步码（留空则不变）。"
    : returning
      ? `检测到本机曾使用「${prefillName}」，填姓名即可继续；若该姓名设过同步码，需要填对才能进入。`
      : "请填写真实姓名。同步码选填——多台设备填同一个码，提取记录就会合并在一起。");
  paintIdentityChoices(state.memberIdentity || "");
  modal.classList.remove("hidden");
  setTimeout(() => (loggedIn ? code : input).focus(), 50);
  // 月度数字异步填充（失败静默，不阻塞登录）
  const slot = el("nameMonthlySlot");
  if (slot) {
    slot.innerHTML = "";
    fetchMonthlySummary().then((data) => { slot.innerHTML = monthlySummaryHtml(data); }).catch(() => {});
  }
}

function paintIdentityChoices(identity) {
  document.querySelectorAll("#identityChoices .identity-btn").forEach((btn) => {
    const active = btn.dataset.identity === identity;
    btn.classList.toggle("selected", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

/** 点选身份（不落盘，提交时一起发送） */
export function selectIdentity(identity) {
  state.memberIdentity = identity;
  paintIdentityChoices(identity);
}

export function closeNameModal() {
  const modal = el("nameModal");
  if (modal) modal.classList.add("hidden");
}

export function skipMemberName() {
  closeNameModal();
  showToast("未设置姓名，可随时在统计页填写");
}

/** 进入 / 注册 / 改码：服务端一个入口 */
export async function submitSession() {
  const name = (el("memberNameInput")?.value || "").trim();
  const code = (el("memberPinInput")?.value || "").trim();
  const identity = state.memberIdentity || "";
  if (!name) { showToast("请输入姓名"); return; }
  if (code && !/^\d{4}$/.test(code)) { showToast("同步码需为 4 位数字"); return; }
  if (!identity) { showToast("请选择身份"); return; }
  try {
    const r = await apiFetch("/api/session", {
      method: "POST",
      body: JSON.stringify({ name, code, identity }),
    });
    applySession(r);
    closeNameModal();
    showToast(state.memberName && r.name === state.memberName ? "已保存" : `已进入：${r.name}`);
    // 统计页若开着，立刻刷新姓名/身份展示
    if (!el("tab-stats").classList.contains("hidden")) loadStats().catch(() => {});
  } catch (e) {
    showToast(e.message || "操作失败，请稍后再试");
  }
}

/** 保存服务端签发的凭证（姓名即设备标识） */
function applySession(r) {
  state.memberName = r.name || "";
  state.memberIdentity = r.identity || "";
  localStorage.setItem("member_name", state.memberName);
  localStorage.setItem("member_identity", state.memberIdentity);
  if (r.device_id && r.device_sig) {
    state.deviceId = r.device_id;
    state.deviceSig = r.device_sig;
    localStorage.setItem("device_id", r.device_id);
    localStorage.setItem("device_sig", r.device_sig);
  }
}

/** 退出：清本地凭证 → 换匿名身份 → 重新弹引导框 */
export async function logout() {
  const fresh = await apiFetch("/api/session", { method: "DELETE" }).catch(() => null);
  state.memberName = "";
  state.memberIdentity = "";
  localStorage.removeItem("member_name");
  localStorage.removeItem("member_identity");
  localStorage.removeItem("device_id");
  localStorage.removeItem("device_sig");
  state.deviceId = "";
  state.deviceSig = "";
  if (fresh && fresh.device_id && fresh.device_sig) {
    // 用服务端新发的匿名设备继续（不清的话下次请求会再签一次，效果相同）
    state.deviceId = fresh.device_id;
    state.deviceSig = fresh.device_sig;
    localStorage.setItem("device_id", fresh.device_id);
    localStorage.setItem("device_sig", fresh.device_sig);
  }
  showToast("已退出，提取记录将不再与此人关联");
  openLoginModal("");
}
