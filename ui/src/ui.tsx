// Small interaction primitives: popovers and menus, URL-synced tabs, toasts, the theme,
// keyboard shortcuts, and loading skeletons.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { Icon, type IconName } from "./icons";

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
  return (
    <div className="pop" ref={ref}>
      {trigger(open, () => setOpen((o) => !o))}
      {open && (
        <div className={`pop-panel ${align === "right" ? "align-right" : ""}`} role="menu" aria-label={label}>
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

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}
