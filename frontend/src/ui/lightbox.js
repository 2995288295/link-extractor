import { state } from "../core/state.js";

export function openCoverPreview(src, alt) {
  if (!src) return;
  const modal = document.getElementById("coverPreview");
  const image = document.getElementById("coverPreviewImage");
  const title = document.getElementById("coverPreviewTitle");
  state.coverPreviewTrigger = document.activeElement;
  state.coverPreviewBodyOverflow = document.body.style.overflow;
  image.src = src;
  image.alt = alt || "内容封面";
  title.textContent = (alt || "内容封面") + "预览";
  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  document.getElementById("coverPreviewClose").focus();
}

export function closeCoverPreview() {
  const modal = document.getElementById("coverPreview");
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  document.getElementById("coverPreviewImage").removeAttribute("src");
  document.body.style.overflow = state.coverPreviewBodyOverflow;
  if (state.coverPreviewTrigger && typeof state.coverPreviewTrigger.focus === "function") state.coverPreviewTrigger.focus();
  state.coverPreviewTrigger = null;
}

export function closeCoverPreviewOnBackdrop(event) {
  if (event.target === event.currentTarget) closeCoverPreview();
}

document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") closeCoverPreview();
});