// Small interaction primitives: popovers and menus, URL-synced tabs, toasts, the theme,
// keyboard shortcuts, loading skeletons, and following live content to the bottom.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { useSearchParams } from "react-router-dom";
import { Icon, type IconName } from "./icons";
import { applyPopoverLayout } from "./popover";

// ---- popover / menu ----------------------------------------------------------------------

export function Popover({
  trigger,
  children,
  align = "left",
  label,
}: {
  trigger: (open: boolean, toggle: () => void) => ReactNode;
  children: (close: () => void) => ReactNode;
  align?: "left" | "right";
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  useLayoutEffect(() => {
    if (!open) return;
    const anchor = ref.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    const reposition = (measureWidth = true) => applyPopoverLayout(anchor, panel, align, measureWidth);
    reposition(true);
    const onResize = () => reposition(true);
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && panel.contains(e.target)) return;
      reposition(false);
    };
    window.addEventListener("resize", onResize, { passive: true });
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    const resizes = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => reposition(true));
    resizes?.observe(panel);
    resizes?.observe(anchor);
    const bodyClass = typeof MutationObserver === "undefined" ? null : new MutationObserver(() => reposition(true));
    bodyClass?.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onScroll, { capture: true });
      resizes?.disconnect();
      bodyClass?.disconnect();
    };
  }, [open, align]);
  return (
    <div className="pop" ref={ref}>
      {trigger(open, () => setOpen((o) => !o))}
      {open && (
        <div
          ref={panelRef}
          className={`pop-panel ${align === "right" ? "align-right" : ""}`}
          role="menu"
          aria-label={label}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

export function MenuItem({
  icon,
  children,
  onClick,
  hint,
  checked,
  href,
}: {
  icon?: IconName;
  children: ReactNode;
  onClick?: () => void;
  hint?: ReactNode;
  checked?: boolean;
  href?: string;
}) {
  const inner = (
    <>
      {checked !== undefined && <span className="menu-check">{checked ? <Icon name="check" size={14} /> : null}</span>}
      {icon && <Icon name={icon} />}
      <span>{children}</span>
      {hint && <span className="right">{hint}</span>}
    </>
  );
  if (href) {
    return (
      <a className="menu-item" role="menuitem" href={href} onClick={onClick} target="_blank" rel="noreferrer">
        {inner}
      </a>
    );
  }
  return (
    <button className="menu-item" role="menuitem" onClick={onClick}>
      {inner}
    </button>
  );
}

// ---- tabs ------------------------------------------------------------------------------

export interface TabDef {
  id: string;
  label: ReactNode;
  count?: number;
}

/** Tabs whose selection lives in the URL (``?tab=``), so links and the back button keep it. */
export function useTab(tabs: TabDef[], param = "tab"): [string, (id: string) => void] {
  const [params, setParams] = useSearchParams();
  const current = params.get(param);
  const active = tabs.some((t) => t.id === current) ? (current as string) : tabs[0].id;
  const set = (id: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id === tabs[0].id) next.delete(param);
        else next.set(param, id);
        return next;
      },
      { replace: true },
    );
  return [active, set];
}

export function Tabs({ tabs, active, onChange }: { tabs: TabDef[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={t.id === active} className={t.id === active ? "active" : ""} onClick={() => onChange(t.id)}>
          {t.label}
          {t.count ? <span className="n">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

// ---- toasts --------------------------------------------------------------------------------

interface Toast {
  id: number;
  title: string;
  body?: ReactNode;
  icon?: IconName;
  action?: { label: string; onClick: () => void };
}

const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((all) => [...all.slice(-3), { ...t, id }]);
    window.setTimeout(() => setToasts((all) => all.filter((x) => x.id !== id)), 7000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast">
            {t.icon && <Icon name={t.icon} size={18} />}
            <div className="body">
              <div className="t">{t.title}</div>
              {t.body && <div className="small muted">{t.body}</div>}
            </div>
            {t.action && (
              <button
                className="sm"
                onClick={() => {
                  t.action?.onClick();
                  setToasts((all) => all.filter((x) => x.id !== t.id));
                }}
              >
                {t.action.label}
              </button>
            )}
            <button className="ghost icon sm" aria-label="Dismiss" onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}>
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}

// ---- theme ----------------------------------------------------------------------------------

export type Theme = "system" | "light" | "dark";

function readTheme(): Theme {
  try {
    const t = localStorage.getItem("am.theme");
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("am.theme", theme);
    } catch {
      // storage can be unavailable; the theme then lasts for this page only
    }
  }, [theme]);
  return [theme, setTheme];
}

// ---- keyboard ------------------------------------------------------------------------------

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/** Call ``handler`` for ``key`` (``mod+k`` means Ctrl or Cmd); plain keys are ignored while typing. */
export function useHotkey(key: string, handler: (e: KeyboardEvent) => void, enabled = true) {
  const saved = useRef(handler);
  saved.current = handler;
  useEffect(() => {
    if (!enabled) return;
    const want = key.toLowerCase();
    const mod = want.startsWith("mod+");
    const bare = mod ? want.slice(4) : want;
    const onKey = (e: KeyboardEvent) => {
      if (mod !== (e.metaKey || e.ctrlKey)) return;
      if (!mod && (typing(e.target) || e.altKey)) return;
      if (e.key.toLowerCase() !== bare) return;
      e.preventDefault();
      saved.current(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [key, enabled]);
}

// ---- skeleton ------------------------------------------------------------------------------

/** Keeps the scrolling box ``ref`` at its bottom while its content grows (new steps, or
 * text streaming into one) as long as the reader is at the bottom. Scrolling up to read
 * stops it; scrolling back down resumes it. Returns ``stick``, which scrolls to the bottom
 * and resumes following, for when the reader acts (sends a message). */
export function useFollowBottom(ref: RefObject<HTMLElement | null>): () => void {
  const following = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    };
    const follow = () => {
      if (following.current) el.scrollTop = el.scrollHeight;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    const changes = new MutationObserver(follow);
    changes.observe(el, { childList: true, subtree: true, characterData: true });
    const resizes = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(follow);
    resizes?.observe(el);
    follow();
    return () => {
      el.removeEventListener("scroll", onScroll);
      changes.disconnect();
      resizes?.disconnect();
    };
  }, [ref]);
  return useCallback(() => {
    following.current = true;
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ref]);
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}
