// Copying text to the clipboard. The Clipboard API exists only on secure pages (HTTPS,
// or localhost); a dashboard opened over plain http from another machine, which is how
// a host on a local network is usually reached, has none. There the older
// select-and-copy command still works, as long as it runs inside the click.

const GITHUB_DEVICE_CODE = /^[A-Z0-9]{4}-[A-Z0-9]{4}$/i;

/** The text to put on the clipboard for a one-time device code. GitHub's verification
 * page splits the 8 characters across 8 separate inputs with a fixed hyphen between
 * boxes 4 and 5; stripping the hyphen avoids mobile browsers truncating or shifting
 * boxes while still working identically on desktop. */
export function copyableDeviceCode(code: string, site = "GitHub"): string {
  const trimmed = code.trim();
  return site === "GitHub" && GITHUB_DEVICE_CODE.test(trimmed) ? trimmed.replace(/-/g, "") : trimmed;
}

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

/** Copy ``text`` from a link click that immediately opens another tab. A new tab can
 * steal focus on mobile (notably iOS Safari) before an async ``writeText`` resolves,
 * so we copy synchronously during the click first and also fire ``writeText`` when
 * available. */
export async function copyBeforeOpen(text: string): Promise<boolean> {
  const syncOk = selectAndCopy(text);
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return syncOk;
    }
  }
  return syncOk;
}

function selectAndCopy(text: string): boolean {
  const area = document.createElement("textarea");
  area.value = text;
  // On iOS Safari, ``readonly`` blocks programmatic selection in a textarea, and a
  // font-size below 16px can trigger viewport auto-zoom on focus.
  area.contentEditable = "true";
  area.readOnly = false;
  area.setAttribute("aria-hidden", "true");
  Object.assign(area.style, {
    position: "fixed",
    top: "0",
    left: "0",
    width: "2em",
    height: "2em",
    padding: "0",
    border: "0",
    outline: "none",
    fontSize: "16px",
    opacity: "0",
    pointerEvents: "none",
  });
  const focused = document.activeElement as HTMLElement | null;
  document.body.appendChild(area);
  let ok = false;
  try {
    area.focus({ preventScroll: true });
    area.select();
    const sel = window.getSelection();
    if (sel) {
      const range = document.createRange();
      range.selectNodeContents(area);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    area.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  focused?.focus?.({ preventScroll: true });
  return ok;
}
