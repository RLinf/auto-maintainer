// Host setup in the browser: the address, the GitHub CLI login the service acts as, the
// memory repository, and the service's own repository. The service reaches GitHub only
// through the GitHub CLI; when the host has none, the page installs it. On a new host,
// whoever proves through GitHub that they maintain the service's repository owns the
// setup; later, maintainers reach this page from Settings.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import { ErrorBox, Status, useAct } from "../components";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { Logo } from "../logo";
import { Skeleton } from "../ui";

interface Person {
  login: string;
  name: string;
  avatar_url: string;
  kind?: "gh";
  warning?: string;
}

interface SetupStatus {
  first_run: boolean;
  mine: boolean;
  busy?: string;
  code: boolean;
  configured: boolean;
  base_url: string;
  identity: Person | null;
  claimer: Person | null;
  /** How the host reached GitHub before only the GitHub CLI remained, if it must connect again. */
  reconnect: string | null;
  memory: string | null;
  code_repo: string | null;
  approve_permission: string;
  detected: {
    gh: { installed: boolean; user?: Person; error?: string };
    agents: Record<string, boolean>;
    checkout_repo: string | null;
    hostname: string;
  };
}

const AGENT_NAMES: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
  opencode: "OpenCode",
  gemini: "Gemini CLI",
};

export default function SetupPage({ firstRun = false }: { firstRun?: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["setup"],
    queryFn: () => api.get<SetupStatus>("/api/setup/status"),
    retry: false,
  });
  const [baseUrl, setBaseUrl] = useState("");
  const [memoryRepo, setMemoryRepo] = useState("");
  const [codeRepo, setCodeRepo] = useState("");
  const [showOther, setShowOther] = useState(false);
  const s = status.data;
  useEffect(() => {
    if (!s || s.busy) return;
    setBaseUrl((v) => v || (s.configured ? s.base_url : window.location.origin));
    setCodeRepo((v) => v || s.code_repo || s.detected.checkout_repo || "");
    const login = s.claimer?.login ?? s.identity?.login;
    setMemoryRepo((v) => v || s.memory || (login ? `${login}/auto-maintainer-memory` : ""));
  }, [s]);
  const refresh = () => client.invalidateQueries({ queryKey: ["setup"] });
  const hostLogin = useAct(() => api.post<Person>("/api/setup/github"), [["setup"]]);
  const memory = useAct((body: { repo: string | null; create: boolean }) => api.post<{ repo: string | null; created?: boolean }>("/api/setup/memory", body), [["setup"]]);
  const finish = useAct(() => api.post<{ redirect: string }>("/api/setup/finish", { base_url: baseUrl, code_repo: codeRepo || null }), []);

  const shell = (children: ReactNode) => (
    <div style={firstRun ? { minHeight: "100vh", display: "flex", justifyContent: "center", padding: "48px 16px 96px" } : { display: "flex", justifyContent: "center" }}>
      <div style={{ width: "min(760px, 100%)", display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="row" style={{ gap: 12 }}>
          <Logo size={44} />
          <div className="stack" style={{ gap: 2 }}>
            <h1>{firstRun ? t("Set up auto-maintainer") : t("Host setup")}</h1>
            <span className="muted">
              {firstRun
                ? t("Everything happens here and on GitHub. Nothing to type in a terminal, no files to edit.")
                : t("Change how this host connects to GitHub. The service restarts on the new setup when you save.")}
            </span>
          </div>
        </div>
        {children}
      </div>
    </div>
  );

  if (status.isLoading) return shell(<div className="card"><Skeleton rows={6} /></div>);
  if (status.error || !s) {
    return shell(<div className="card"><ErrorBox error={status.error ?? new ApiError(500, t("No answer"))} /></div>);
  }
  if (s.busy) {
    return shell(
      <div className="card stack">
        <strong>
          {t("Someone is already setting this host up:")} {s.busy}
        </strong>
        <span className="muted">{t("Only they can finish it. When they are done, sign in here with GitHub.")}</span>
      </div>,
    );
  }

  const id = s.identity;
  const owner = s.claimer;
  const proven = !s.first_run || s.code || !!owner;
  const ghUser = s.detected.gh.user;
  const repo = s.detected.checkout_repo;

  return shell(
    <>
      {s.reconnect && (
        <div className="notice">
          <Icon name="shield" size={14} />{" "}
          {t("This host reached GitHub through")} {s.reconnect === "a GitHub App" ? t("a GitHub App") : t("a token")}
          {t(", which is no longer supported. Connect the GitHub CLI below; your other answers are kept.")}
        </div>
      )}
      {owner && (
        <div className="strip dev">
          {owner.avatar_url ? <img className="avatar" src={owner.avatar_url} alt="" /> : <Icon name="shield" />}
          <span>
            {t("Signed in as")} <strong>{owner.login}</strong>
            {repo && (
              <>
                {" "}
                · {t("a maintainer of")} <code>{repo}</code>
              </>
            )}
          </span>
        </div>
      )}
      {s.first_run && !owner && !s.code && (
        <div className="notice small">
          <Icon name="shield" size={13} />{" "}
          {repo
            ? `${t("Only a maintainer of")} ${repo} ${t("can set this service up; you prove it by signing in with GitHub below.")}`
            : t("This checkout has no GitHub repository, so ownership cannot be checked on GitHub. Open the setup link printed when the service started.")}
        </div>
      )}

      <Step n={1} title={t("Address")} done={!!baseUrl} hint={t("What browsers use to reach this service; links in alerts point here.")}>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://your-host:7777" style={{ width: "100%" }} />
      </Step>

      <Step
        n={2}
        title={t("Connect GitHub")}
        done={!!id && proven}
        hint={t("The account the service acts as, through the GitHub CLI: it reads pull requests and CI, and posts what maintainers approve. Everyone signs in the same way, with a one-time code on github.com.")}
      >
        {id && (
          <div className="row">
            {id.avatar_url ? <img className="avatar" src={id.avatar_url} alt="" /> : <Icon name="cpu" />}
            <strong>{id.login}</strong>
            <span className="muted small">{t("through the GitHub CLI")}</span>
            {id.warning && <span className="small" style={{ color: "var(--warn)" }}>{id.warning}</span>}
            {!showOther && (
              <button className="ghost sm right" onClick={() => setShowOther(true)}>
                {t("Change")}
              </button>
            )}
          </div>
        )}
        {(!id || showOther) && (
          <div className="choices">
            {s.detected.gh.installed ? (
              <GhWebLogin onDone={() => (setShowOther(false), void refresh())} />
            ) : (
              <GhInstall path="/api/setup/gh/install" onDone={() => void refresh()} />
            )}
            {s.code && ghUser && (
              <Choice
                icon="cpu"
                title={t("The GitHub CLI login already on this host")}
                body={
                  <>
                    {t("Logged in as")} <strong>{ghUser.login}</strong>.
                  </>
                }
                action={
                  <button className="sm" disabled={hostLogin.isPending} onClick={() => hostLogin.mutate(undefined, { onSuccess: () => setShowOther(false) })}>
                    {t("Use")} {ghUser.login}
                  </button>
                }
              />
            )}
          </div>
        )}
        <ErrorBox error={hostLogin.error} />
      </Step>

      <Step n={3} title={t("Memory repository")} done={!!s.memory || memory.data?.repo === null} hint={t("A private repository where the service keeps profiles, settings, and what it learns, as reviewable commits.")}>
        {s.memory && <Status value="executed" label={s.memory} />}
        <div className="row">
          <input value={memoryRepo} onChange={(e) => setMemoryRepo(e.target.value)} placeholder="owner/auto-maintainer-memory" style={{ flex: 1, minWidth: 220 }} />
          <button className="primary sm" disabled={!memoryRepo || memory.isPending || !proven || !id} onClick={() => memory.mutate({ repo: memoryRepo, create: true })}>
            {t("Create or use")}
          </button>
          <button className="ghost sm" disabled={memory.isPending || !proven} onClick={() => memory.mutate({ repo: null, create: false })}>
            {t("Keep it on this host")}
          </button>
        </div>
        {memory.data && (
          <span className="small muted">
            {memory.data.repo ? (memory.data.created ? t("Created a private repository.") : t("Using the private repository.")) : t("Memory stays on this host.")}
          </span>
        )}
        <ErrorBox error={memory.error} />
      </Step>

      <Step n={4} title={t("The service's own repository")} done={!!codeRepo} optional hint={t("Its maintainers own the service settings and approve the changes the service makes to itself.")}>
        <input value={codeRepo} onChange={(e) => setCodeRepo(e.target.value)} placeholder="owner/auto-maintainer" style={{ width: "100%" }} />
      </Step>

      <section className="card stack">
        <div className="row">
          <strong>{t("Agents found on this host")}</strong>
          <span className="small muted">{t("Choose agents and model providers on the Settings page later.")}</span>
        </div>
        <div className="row">
          {Object.entries(s.detected.agents).map(([name, found]) => (
            <Status key={name} value={found ? "executed" : "cancelled"} label={`${AGENT_NAMES[name] ?? name}${found ? "" : ` · ${t("not found")}`}`} />
          ))}
        </div>
      </section>

      <div className="row" style={{ justifyContent: "flex-end" }}>
        <ErrorBox error={finish.error} />
        <button
          className="primary"
          style={{ height: 40, padding: "0 18px" }}
          disabled={!baseUrl || !(id || (s.configured && !s.first_run && !s.reconnect)) || !proven || finish.isPending}
          onClick={() =>
            finish.mutate(undefined, {
              onSuccess: (r) => {
                window.location.href = r.redirect;
              },
            })
          }
        >
          {finish.isPending ? t("Starting the service…") : s.first_run ? t("Finish and start") : t("Save and restart the service")}
        </button>
      </div>
    </>,
  );
}

/** Install the GitHub CLI on a host that has none: the service downloads GitHub's own
 * release into its data folder and checks it against the release's checksums. */
export function GhInstall({ path, onDone }: { path: string; onDone: () => void }) {
  const t = useT();
  const install = useMutation({ mutationFn: () => api.post<{ installed: boolean }>(path), onSuccess: onDone });
  return (
    <Choice
      icon="download"
      title={t("Install the GitHub CLI")}
      body={t(
        "The service reaches GitHub, and people sign in, through the GitHub CLI (gh), which this host does not have yet. Installing downloads GitHub's own release into the service's data folder and checks it against the release's checksums; nothing else on the host changes.",
      )}
      action={
        <div className="stack" style={{ gap: 8 }}>
          <button className="primary sm" disabled={install.isPending} onClick={() => install.mutate()}>
            <Icon name="download" size={14} />
            {install.isPending ? t("Installing…") : t("Install the GitHub CLI")}
          </button>
          <ErrorBox error={install.error} />
        </div>
      }
    />
  );
}

/** Log the service's GitHub CLI in from the browser: show GitHub's one-time code and
 * wait until the person has entered it on github.com. */
export function GhWebLogin({
  onDone,
  start: startPath = "/api/setup/gh/start",
  poll: pollPath = "/api/setup/gh/poll",
  title,
  body,
}: {
  onDone: () => void;
  start?: string;
  poll?: string;
  title?: string;
  body?: string;
}) {
  const t = useT();
  // The sign-in in progress outlives this component: going to GitHub in the same tab,
  // a reload, or the page being drawn again brings back the code and keeps waiting.
  const [code, setCode] = useState<GhFlow | null>(() => loadFlow(pollPath));
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);
  const resumed = useRef(code !== null);
  const timer = useRef<number | null>(null);
  const reset = () => {
    saveFlow(pollPath, null);
    setCode(null);
  };
  const poll = (flowId?: string) => {
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.post<{ state?: string; pending?: boolean; error?: string; login?: string }>(pollPath, flowId ? { flow_id: flowId } : {});
        if (r.state === "pending" || r.pending) return poll(flowId);
        reset();
        if (r.state === "done" || r.login) return onDone();
        // "idle": the server no longer knows this sign-in (it restarted); start again.
        if (r.state === "idle" && resumed.current) return;
        setError(new Error(r.error || t("The sign-in did not finish; try again.")));
      } catch (e) {
        if (e instanceof ApiError && e.status === 202) return poll(flowId);
        reset();
        if (!(e instanceof ApiError && e.status === 404 && resumed.current)) setError(e);
      }
    }, 2000);
  };
  useEffect(() => {
    if (code) poll(code.flow_id);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
    // Resume once, for a sign-in that was already waiting when the page was drawn.
  }, []);
  const start = async () => {
    setError(null);
    try {
      const r = await api.post<{ code: string; url: string; flow_id?: string }>(startPath);
      const flow = { ...r, exp: Date.now() + FLOW_MS };
      saveFlow(pollPath, flow);
      resumed.current = false;
      setCode(flow);
      poll(r.flow_id);
    } catch (e) {
      setError(e);
    }
  };
  const copy = () => {
    if (!code) return;
    void navigator.clipboard?.writeText(code.code).then(
      () => setCopied(true),
      () => undefined,
    );
  };
  return (
    <Choice
      icon="globe"
      title={title ?? t("Sign in with GitHub")}
      body={
        body ??
        t("You enter a one-time code on github.com, and the service acts as the account you authorize. For a team, authorize an account made for the service, so what it posts is not under your name.")
      }
      action={
        <div className="stack" style={{ gap: 8 }}>
          {code ? (
            <>
              <div className="row" style={{ gap: 8 }}>
                <span className="code" style={{ fontSize: 22 }}>
                  {code.code}
                </span>
                <button className="ghost sm" onClick={copy}>
                  {copied ? t("Copied") : t("Copy")}
                </button>
              </div>
              <a className="button primary" href={code.url} target="_blank" rel="noopener noreferrer" onClick={copy}>
                {t("Copy the code and open GitHub")} <Icon name="external" size={12} />
              </a>
              <span className="small muted row" style={{ gap: 6 }}>
                <span className="typing" aria-hidden>
                  <span />
                  <span />
                  <span />
                </span>
                {t("Waiting for you to authorize on GitHub. Paste the code there; this page keeps it, and finishes by itself once you authorize, even if you come back to it later.")}
              </span>
              <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => void start()}>
                {t("Get a new code")}
              </button>
            </>
          ) : (
            <button className="primary sm" onClick={() => void start()}>
              {t("Sign in with GitHub")}
            </button>
          )}
          <ErrorBox error={error} />
        </div>
      }
    />
  );
}

/** How long a sign-in code is worth showing again: GitHub's device codes last 15 minutes. */
const FLOW_MS = 14 * 60 * 1000;

interface GhFlow {
  code: string;
  url: string;
  flow_id?: string;
  exp: number;
}

function loadFlow(pollPath: string): GhFlow | null {
  try {
    const flow = JSON.parse(sessionStorage.getItem(`am.gh-login:${pollPath}`) ?? "null") as GhFlow | null;
    return flow && flow.exp > Date.now() ? flow : null;
  } catch {
    return null;
  }
}

function saveFlow(pollPath: string, flow: GhFlow | null) {
  try {
    if (flow) sessionStorage.setItem(`am.gh-login:${pollPath}`, JSON.stringify(flow));
    else sessionStorage.removeItem(`am.gh-login:${pollPath}`);
  } catch {
    // Without storage the sign-in still works while the page stays open.
  }
}

function Step({ n, title, hint, done, optional, children }: { n: number; title: string; hint?: string; done: boolean; optional?: boolean; children: ReactNode }) {
  const t = useT();
  return (
    <section className="card stack" style={{ gap: 12 }}>
      <div className="row" style={{ gap: 10, alignItems: "flex-start", flexWrap: "nowrap" }}>
        <span className={`step-dot ${done ? "done" : ""}`}>{done ? <Icon name="check" size={13} /> : n}</span>
        <div className="stack" style={{ gap: 2 }}>
          <strong style={{ fontSize: 15 }}>
            {title} {optional && <span className="muted small">({t("optional")})</span>}
          </strong>
          {hint && <span className="small muted">{hint}</span>}
        </div>
      </div>
      {children}
    </section>
  );
}

export function Choice({ icon, title, body, action, recommended }: { icon: IconName; title: string; body: ReactNode; action: ReactNode; recommended?: boolean }) {
  const t = useT();
  return (
    <div className="choice">
      <div className="row" style={{ gap: 8 }}>
        <Icon name={icon} />
        <strong>{title}</strong>
        {recommended && <span className="pill status-info">{t("recommended")}</span>}
      </div>
      <span className="small muted">{body}</span>
      <div>{action}</div>
    </div>
  );
}
