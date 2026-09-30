// Internal deployments use HTTP, where navigator.clipboard may be unavailable.
export async function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* use the selected-text fallback */ }
  }
  const previous = document.activeElement;
  const input = document.createElement('textarea');
  input.value = text; input.setAttribute('readonly', '');
  input.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(input); input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (previous instanceof HTMLElement) previous.focus();
  if (!copied) throw new Error('请选中下方指令并手动复制');
}
