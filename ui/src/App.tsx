// The shell: sign-in gate, a top bar with three primary destinations and everything else
// one menu or one keystroke away (Cmd/Ctrl+K), the agent on every page (Cmd/Ctrl+J), and
// the routes.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, type Me } from "./api";
import { AgentProvider, useAgent } from "./agent";
import { Empty, ErrorBox } from "./components";
import { useMe, useMemoryChanges, useProposals } from "./data";
import { QUESTION_EVENT, useLiveEvents } from "./events";
import { JobDrawer } from "./jobs";
import { useLang, useT } from "./i18n";
import { Icon } from "./icons";
import { Logo } from "./logo";
import { CommandPalette, NAV, type Command } from "./palette";
import AgentsPage from "./pages/Agents";
import AuditPage from "./pages/Audit";
import ChatPage from "./pages/Chat";
import CiPage from "./pages/Ci";
import HomePage from "./pages/Home";
import InboxPage from "./pages/Inbox";
import MemoryPage from "./pages/Memory";
import PrPage from "./pages/Pr";
import QuestionsPage from "./pages/Questions";
import QueuePage from "./pages/Queue";
import { RepoDetailPage, ReposPage } from "./pages/Repos";
import ServicePage from "./pages/Service";
import SettingsPage from "./pages/Settings";
import SetupPage from "./pages/Setup";
import SignIn from "./pages/SignIn";
import { MenuItem, Popover, Skeleton, ToastProvider, useHotkey, useTheme, useToast } from "./ui";

export { useMe } from "./data";

export default function App() {
  const me = useMe();
  if (me.isLoading) {
    return (
      <div className="content" style={{ paddingTop: 80 }}>
        <Skeleton rows={6} />
      </div>
    );
  }
  if (me.error instanceof ApiError && me.error.status === 401) return <SignIn />;
  if (me.error instanceof ApiError && me.error.status === 503 && me.error.message.includes("setup")) return <SetupPage firstRun />;
  if (me.error) {
    return (
      <div className="content">
        <ErrorBox error={me.error} />
      </div>
    );
  }
  return (
    <ToastProvider>
      <AgentProvider>
        <Shell me={me.data!} />
      </AgentProvider>
    </ToastProvider>
  );
}


function Shell({ me }: { me: Me }) {
  const t = useT();
  const { lang, setLang } = useLang();
  const [theme, setTheme] = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const agent = useAgent();
  const toast = useToast();
  const client = useQueryClient();
  useLiveEvents(true, me.login);
  const proposals = useProposals();
  const memory = useMemoryChanges();
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);

  const counts = {
    questions: me.pending_questions,
    proposals: proposals.data?.length ?? 0,
    memory: memory.data?.length ?? 0,
  };
  const waiting = counts.questions + counts.proposals + counts.memory;

  useHotkey("mod+k", () => setPalette((o) => !o));
  useHotkey("?", () => setHelp(true));
  useGoKeys((to) => navigate(to));
  useEffect(() => {
    const onAsk = (e: Event) =>
      toast({
        title: t("An agent is waiting for an answer"),
        body: (e as CustomEvent<string>).detail,
        icon: "question",
        action: { label: t("Answer"), onClick: () => navigate("/questions") },
      });
    window.addEventListener(QUESTION_EVENT, onAsk);
    return () => window.removeEventListener(QUESTION_EVENT, onAsk);
  }, [toast, navigate, t]);

  const signOut = async () => {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/";
  };
  const setPaused = async (paused: boolean) => {
    try {
      await api.post(paused ? "/api/pause" : "/api/resume", paused ? { reason: "paused from the dashboard" } : {});
      await client.invalidateQueries({ queryKey: ["me"] });
      toast({ title: paused ? t("The service is paused") : t("The service resumed"), icon: paused ? "pause" : "play" });
    } catch (e) {
      toast({ title: t("Could not change the service state"), body: e instanceof Error ? e.message : String(e), icon: "x" });
    }
  };
  const canNotify = typeof Notification !== "undefined" && Notification.permission === "default";

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [
      { id: "agent", group: "Commands", label: t("Ask the agent about this page"), sub: "⌘J", icon: "sparkles", run: () => agent.open() },
      me.paused
        ? { id: "resume", group: "Commands", label: t("Resume the service"), icon: "play", run: () => void setPaused(false) }
        : { id: "pause", group: "Commands", label: t("Pause the service"), icon: "pause", keywords: "stop halt", run: () => void setPaused(true) },
      { id: "theme-light", group: "Commands", label: t("Use the light theme"), icon: "sun", keywords: "appearance", run: () => setTheme("light") },
      { id: "theme-dark", group: "Commands", label: t("Use the dark theme"), icon: "moon", keywords: "appearance", run: () => setTheme("dark") },
      { id: "theme-system", group: "Commands", label: t("Use the system theme"), icon: "monitor", keywords: "appearance", run: () => setTheme("system") },
      {
        id: "lang",
        group: "Commands",
        label: lang === "zh" ? "Switch to English" : "切换到中文",
        icon: "globe",
        keywords: "language chinese english",
        run: () => setLang(lang === "zh" ? "en" : "zh"),
      },
      { id: "keys", group: "Commands", label: t("Keyboard shortcuts"), sub: "?", icon: "keyboard", run: () => setHelp(true) },
      { id: "signout", group: "Commands", label: t("Sign out"), icon: "logout", run: () => void signOut() },
    ];
    if (canNotify) {
      list.push({ id: "notify", group: "Commands", label: t("Alert me in the browser"), icon: "bell", run: () => void Notification.requestPermission() });
    }
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me.paused, lang, agent, t, canNotify]);

  return (
    <>
      <header className="header">
        <div className="header-inner">
          <Link to="/" className="logo" aria-label="auto-maintainer home">
            <Logo size={28} />
            <span className="name">auto-maintainer</span>
          </Link>
          <TopNav waiting={waiting} counts={counts} />
          <button className="search-trigger" onClick={() => setPalette(true)} aria-label={t("Search")} title={t("Search or run a command")}>
            <Icon name="search" />
            <span className="grow">{t("Search")}</span>
            <span className="kbd">⌘K</span>
          </button>
          <button className="agent hide-narrow" onClick={() => agent.toggle()} title={`${t("Ask agent")} (⌘J)`}>
            <Icon name="sparkles" />
            {t("Ask agent")}
          </button>
          <Popover
            align="right"
            label={t("Waiting on you")}
            trigger={(_, toggle) => (
              <button className="ghost icon" onClick={toggle} aria-label={`${t("Waiting on you")}: ${waiting}`} style={{ position: "relative" }}>
                <Icon name="bell" />
                {waiting > 0 && (
                  <span className="pill" style={{ position: "absolute", top: 1, right: -2, padding: "0 5px", lineHeight: "16px", fontSize: 10.5, background: "var(--bad)", color: "#fff" }}>
                    {waiting}
                  </span>
                )}
              </button>
            )}
          >
            {(close) => {
              const go = (to: string) => () => {
                navigate(to);
                close();
              };
              return (
                <>
                  <div className="menu-label">{t("Waiting on you")}</div>
                  <MenuItem icon="question" hint={counts.questions || "—"} onClick={go("/questions")}>
                    {t("Agent questions")}
                  </MenuItem>
                  <MenuItem icon="inbox" hint={counts.proposals || "—"} onClick={go("/inbox")}>
                    {t("Approvals")}
                  </MenuItem>
                  <MenuItem icon="book" hint={counts.memory || "—"} onClick={go("/memory")}>
                    {t("Memory changes")}
                  </MenuItem>
                  <div className="menu-sep" />
                  <MenuItem icon="home" onClick={go("/")}>
                    {t("See everything on Home")}
                  </MenuItem>
                  {canNotify && (
                    <MenuItem icon="bell" onClick={() => void Notification.requestPermission()}>
                      {t("Alert me in the browser")}
                    </MenuItem>
                  )}
                </>
              );
            }}
          </Popover>
          <Popover
            align="right"
            label={t("Account")}
            trigger={(_, toggle) => (
              <button className="ghost icon" onClick={toggle} aria-label={t("Account")}>
                {me.avatar_url ? <img className="avatar" src={me.avatar_url} alt="" /> : <Icon name="globe" />}
              </button>
            )}
          >
            {(close) => (
              <>
                <div style={{ padding: "8px 10px 6px" }}>
                  <div style={{ fontWeight: 600 }}>{me.name || me.login}</div>
                  <div className="small muted">
                    @{me.login}
                    {me.operator ? ` · ${t("operator")}` : ""}
                  </div>
                </div>
                <div className="menu-sep" />
                <div className="menu-label">{t("Theme")}</div>
                <MenuItem icon="monitor" checked={theme === "system"} onClick={() => setTheme("system")}>
                  {t("System")}
                </MenuItem>
                <MenuItem icon="sun" checked={theme === "light"} onClick={() => setTheme("light")}>
                  {t("Light")}
                </MenuItem>
                <MenuItem icon="moon" checked={theme === "dark"} onClick={() => setTheme("dark")}>
                  {t("Dark")}
                </MenuItem>
                <div className="menu-label">{t("Language")}</div>
                <MenuItem checked={lang === "en"} onClick={() => setLang("en")}>
                  English
                </MenuItem>
                <MenuItem checked={lang === "zh"} onClick={() => setLang("zh")}>
                  中文
                </MenuItem>
                <div className="menu-sep" />
                <MenuItem icon="settings" hint="g s" onClick={() => (navigate("/settings"), close())}>
                  {t("Settings")}
                </MenuItem>
                <MenuItem icon="keyboard" hint="?" onClick={() => (setHelp(true), close())}>
                  {t("Keyboard shortcuts")}
                </MenuItem>
                <MenuItem icon={me.paused ? "play" : "pause"} onClick={() => (void setPaused(!me.paused), close())}>
                  {me.paused ? t("Resume the service") : t("Pause the service")}
                </MenuItem>
                <MenuItem icon="logout" onClick={() => void signOut()}>
                  {t("Sign out")}
                </MenuItem>
              </>
            )}
          </Popover>
        </div>
      </header>

      <main className="content">
        {me.paused && (
          <div className="strip paused" role="status">
            <Icon name="pause" />
            <span>{t("The service is paused. It posts and changes nothing until it resumes.")}</span>
            {me.operator && (
              <button className="sm right" onClick={() => void setPaused(false)}>
                <Icon name="play" size={14} />
                {t("Resume")}
              </button>
            )}
          </div>
        )}
        {me.dev_mode && (
          <div className="strip dev" role="status">
            <Icon name="shield" />
            <span>{t("Dev mode: repository actions stay in shadow, except on the sandbox repository.")}</span>
          </div>
        )}
        {counts.questions > 0 && location.pathname !== "/questions" && (
          <div className="strip question" role="status">
            <Icon name="question" />
            <span>
              {counts.questions === 1
                ? t("An agent is waiting for an answer")
                : `${counts.questions} ${t("agents are waiting for an answer")}`}
            </span>
            <Link to="/questions" className="right">
              {t("Answer now")}
            </Link>
          </div>
        )}
        <Routes>
          <Route path="/" element={<HomePage me={me} />} />
          <Route path="/prs" element={<QueuePage me={me} />} />
          <Route path="/pr/:owner/:repo/:number" element={<PrPage me={me} />} />
          <Route path="/inbox" element={<InboxPage me={me} />} />
          <Route path="/questions" element={<QuestionsPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/repos" element={<ReposPage />} />
          <Route path="/repos/:owner/:repo" element={<RepoDetailPage />} />
          <Route path="/ci" element={<CiPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/service" element={<ServicePage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:threadId" element={<ChatPage />} />
          <Route
            path="*"
            element={
              <Empty icon="search">
                {t("Page not found.")} <Link to="/">{t("Go home")}</Link>
              </Empty>
            }
          />
        </Routes>
      </main>
      <JobDrawer />

      {!agent.isOpen && (
        <button className="agent fab" onClick={() => agent.open()} aria-label={t("Ask agent")}>
          <Icon name="sparkles" size={18} />
          <span className="fab-label">{t("Ask agent")}</span>
        </button>
      )}
      <CommandPalette open={palette} onClose={() => setPalette(false)} commands={commands} me={me} />
      {help && <ShortcutsDialog onClose={() => setHelp(false)} />}
    </>
  );
}

/** ``g`` then a letter goes to a page (``g h`` home, ``g p`` pull requests, …). */
function useGoKeys(go: (to: string) => void) {
  const armed = useRef(0);
  useEffect(() => {
    const targets = Object.fromEntries(NAV.filter((n) => n.keys).map((n) => [n.keys!.split(" ")[1], n.to]));
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable || e.metaKey || e.ctrlKey || e.altKey) return;
      if (Date.now() - armed.current < 1200 && targets[e.key]) {
        e.preventDefault();
        armed.current = 0;
        go(targets[e.key]);
      } else if (e.key === "g") {
        armed.current = Date.now();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go]);
}

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  useHotkey("escape", onClose);
  const rows: [string[], string][] = [
    [["⌘", "K"], "Search, go anywhere, run a command"],
    [["⌘", "J"], "Open or close the agent"],
    [["/"], "Search the list on this page"],
    [["j", "k"], "Move through a list"],
    [["↵"], "Open the highlighted row"],
    [["x"], "Select the highlighted row"],
    ...NAV.filter((n) => n.keys).map((n) => [n.keys!.split(" "), `${t("Go to")} ${t(n.label)}`] as [string[], string]),
    [["?"], "Show this list"],
  ];
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={t("Keyboard shortcuts")}>
        <div className="dialog-body">
          <div className="row">
            <h2>{t("Keyboard shortcuts")}</h2>
            <button className="ghost icon sm right" onClick={onClose} aria-label={t("Close")}>
              <Icon name="x" size={14} />
            </button>
          </div>
          <dl className="props">
            {rows.map(([keys, label]) => (
              <div key={label} style={{ display: "contents" }}>
                <dt>
                  {keys.map((k) => (
                    <span key={k} className="kbd" style={{ marginRight: 4 }}>
                      {k}
                    </span>
                  ))}
                </dt>
                <dd>{t(label)}</dd>
              </div>
            ))}
          </dl>
          <span className="small muted">{t("On Windows and Linux, use Ctrl for ⌘.")}</span>
        </div>
      </div>
    </div>
  );
}

/** The top bar's pages: as many as fit, in ``NAV`` order, and the rest under More. A
 * hidden copy of every tab measures the widths, so the split follows the window, the
 * language, and the fonts. */
function TopNav({ waiting, counts }: { waiting: number; counts: { questions: number; proposals: number; memory: number } }) {
  const t = useT();
  const { lang } = useLang();
  const navigate = useNavigate();
  const location = useLocation();
  const wrap = useRef<HTMLDivElement>(null);
  const measure = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(3);
  // The More button shows the current page's name when that page is under it, so it
  // is measured with that name.
  const current = NAV.find((n) => n.to !== "/" && location.pathname.startsWith(n.to));
  const badge = (to: string) =>
    to === "/" ? waiting : to === "/inbox" ? counts.proposals : to === "/questions" ? counts.questions : to === "/memory" ? counts.memory : 0;

  useLayoutEffect(() => {
    const box = wrap.current;
    const ruler = measure.current;
    if (!box || !ruler) return;
    const compute = () => {
      const tabs = Array.from(ruler.children) as HTMLElement[];
      const more = tabs.pop()?.offsetWidth ?? 0;
      const style = getComputedStyle(ruler);
      const gap = parseFloat(style.columnGap || "0") || 0;
      const chrome = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
      const room = box.clientWidth - chrome;
      const widths = tabs.map((el) => el.offsetWidth);
      const all = widths.reduce((sum, w, i) => sum + w + (i ? gap : 0), 0);
      if (all <= room) {
        setFit(widths.length);
        return;
      }
      let used = more;
      let n = 0;
      for (const w of widths) {
        if (used + gap + w > room) break;
        used += gap + w;
        n += 1;
      }
      setFit(Math.max(1, n));
    };
    compute();
    const observer = new ResizeObserver(compute);
    observer.observe(box);
    observer.observe(ruler);
    void document.fonts?.ready.then(compute);
    return () => observer.disconnect();
  }, [lang, waiting, counts.questions, counts.proposals, counts.memory, current]);

  const shown = NAV.slice(0, fit);
  const rest = NAV.slice(fit);
  const restActive = rest.find((n) => n.to !== "/" && location.pathname.startsWith(n.to));
  const tab = (n: (typeof NAV)[number]) => (
    <>
      <Icon name={n.icon} size={15} />
      <span className="label-text">{t(n.label)}</span>
      {badge(n.to) > 0 && <span className="count">{badge(n.to)}</span>}
    </>
  );
  return (
    <div className="nav-wrap" ref={wrap}>
      <nav className="tabs-nav" aria-label={t("Main")}>
        {shown.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === "/"} title={t(n.label)}>
            {tab(n)}
          </NavLink>
        ))}
        {rest.length > 0 && (
          <Popover
            label={t("More")}
            trigger={(_, toggle) => (
              <a
                href="#more"
                className={restActive ? "active" : ""}
                onClick={(e) => {
                  e.preventDefault();
                  toggle();
                }}
                aria-haspopup="menu"
              >
                <span className="label-text">{restActive ? t(restActive.label) : t("More")}</span>
                <Icon name="chevronDown" size={14} />
              </a>
            )}
          >
            {(close) => (
              <div style={{ width: 300 }}>
                {rest.map((n) => (
                  <button
                    key={n.to}
                    className="menu-item"
                    style={{ height: "auto", padding: "8px 10px", alignItems: "flex-start" }}
                    onClick={() => {
                      navigate(n.to);
                      close();
                    }}
                  >
                    <Icon name={n.icon} />
                    <span className="stack" style={{ gap: 0 }}>
                      <span style={{ fontWeight: 550 }}>
                        {t(n.label)}
                        {badge(n.to) > 0 && <span className="muted"> · {badge(n.to)}</span>}
                      </span>
                      <span className="small muted">{t(n.hint)}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </Popover>
        )}
      </nav>
      <div className="nav-ruler-box" aria-hidden="true">
      <div className="tabs-nav nav-ruler" ref={measure}>
        {NAV.map((n) => (
          <a key={n.to}>{tab(n)}</a>
        ))}
        <a>
          <span className="label-text">{current ? t(current.label) : t("More")}</span>
          <Icon name="chevronDown" size={14} />
        </a>
      </div>
      </div>
    </div>
  );
}
