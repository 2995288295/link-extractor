import { state } from "../core/state.js";
import { apiFetch } from "../core/api.js";
import { showToast } from "../ui/toast.js";
import { loadStats } from "./stats.js";
import { fetchMonthlySummary, monthlySummaryHtml } from "./summary.js";

// ---------------------------------------------------------------- 成员账号（v1.18.0 登录体系）

/**
 * 姓名 + 4 位 PIN 即账号凭证（v1.18.0，老大拍板：名字当登录凭证 + PIN 防冒填）。
 *
 * 与 v1.14–1.17「纯署名」的区别：登录成功后服务端以**姓名**作为设备标识签发
 * （HMAC），历史/统计/去重全部按人隔离——换设备登录同一姓名即合并，
 * 老数据经一次性迁移继续可见。
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

/** 页面加载时判定登录态：已登录不打扰；未登录弹登录/注册框 */
export function initProfile() {
  apiFetch("/api/profile")
    .then((profile) => {
      if (profile && profile.name) {
        applySession(profile);          // 已登录
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
  const pin = el("memberPinInput");
  if (!modal || !input) return;
  const returning = Boolean(prefillName);
  input.value = prefillName || "";
  if (pin) pin.value = "";
  setText("nameModalTitle", returning ? "登录" : "先设置你的姓名");
  setText("nameModalDesc", returning
    ? `检测到本机曾使用「${prefillName}」，请输入 PIN 登录后查看你的提取记录。`
    : "请填写真实姓名，并设置一个 4 位 PIN（换设备登录用）。");
  paintIdentityChoices(state.memberIdentity || "");
  modal.classList.remove("hidden");
  setTimeout(() => (returning ? pin : input).focus(), 50);
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
  showToast("未登录：可随时在统计页登录");
}

/** 登录 / 注册 / 认领：三种情况服务端一个入口 */
export async function submitSession() {
  const name = (el("memberNameInput")?.value || "").trim();
  const pin = (el("memberPinInput")?.value || "").trim();
  const identity = state.memberIdentity || "";
  if (!name) { showToast("请输入姓名"); return; }
  if (!/^\d{4}$/.test(pin)) { showToast("PIN 需为 4 位数字"); return; }
  if (!identity) { showToast("请选择身份"); return; }
  try {
    const r = await apiFetch("/api/session", {
      method: "POST",
      body: JSON.stringify({ name, pin, identity }),
    });
    applySession(r);
    closeNameModal();
    showToast(`已登录：${r.name} · ${IDENTITY_LABELS[r.identity] || ""}`);
    // 统计页若开着，立刻刷新姓名/身份展示
    if (!el("tab-stats").classList.contains("hidden")) loadStats().catch(() => {});
  } catch (e) {
    showToast(e.message || "登录失败，请稍后再试");
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

/** 退出登录：清本地凭证 → 换匿名身份 → 重新弹登录框 */
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
  showToast("已退出登录");
  openLoginModal("");
}
