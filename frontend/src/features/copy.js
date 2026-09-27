import { showToast } from "../ui/toast.js";

// ---------------------------------------------------------------- 复制

export async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    flashCopied(btn);
  } catch (e) {
    // 非安全上下文（http://IP）时降级
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      flashCopied(btn);
    } catch (e2) {
      showToast("复制失败，请手动选择复制");
    }
    document.body.removeChild(ta);
  }
}

export function flashCopied(btn) {
  const original = btn.textContent;
  btn.textContent = "✅ 已复制";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove("copied");
  }, 1200);
}