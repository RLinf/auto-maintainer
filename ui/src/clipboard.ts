// Copying text to the clipboard. The Clipboard API exists only on secure pages (HTTPS,
// or localhost); a dashboard opened over plain http from another machine, which is how
// a host on a local network is usually reached, has none. There the older
// select-and-copy command still works, as long as it runs inside the click.

/** Copy ``text``; resolves to ``false`` when the browser refused. Call it from a click
 * handler: the fallback must run while the click is still being handled. */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Denied (no permission, or the page lost focus): try the older way.
    }
  }
  return selectAndCopy(text);
}

function selectAndCopy(text: string): boolean {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  Object.assign(area.style, { position: "fixed", top: "0", left: "0", opacity: "0", pointerEvents: "none" });
  const focused = document.activeElement as HTMLElement | null;
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  focused?.focus?.({ preventScroll: true });
  return ok;
}
