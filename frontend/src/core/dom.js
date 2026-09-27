export function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function unescapeHtml(s) {
  const el = document.createElement("div");
  el.innerHTML = s;
  return el.textContent;
}