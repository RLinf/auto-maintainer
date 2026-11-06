// Host setup in the browser: the address, the GitHub account the service acts as, how
// people sign in, the memory repository, and the service's own repository. On a new
// host, whoever proves through GitHub that they maintain the service's repository owns
// the setup; later, maintainers reach this page from Settings.

import { useQuery, useQueryClient } from "@tanstack/react-query";
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
  kind?: "gh" | "token" | "app";
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
  app: { slug: string; owner: string; install_url: string; purpose: string } | null;
  auth: "gh" | "token" | "app";
  signin: "gh" | "app";
  memory: string | null;
  code_repo: string | null;
  approve_permission: string;
  token_url: string;
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

function postToGitHub(url: string, manifest: string) {
  const form = document.createElement("form");
  form.method = "post";
  form.action = url;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "manifest";
  input.value = manifest;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}

export default function SetupPage({ firstRun = false }: { firstRun?: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["setup"],
    queryFn: () => api.get<SetupStatus>("/api/setup/status"),
    retry: false,
  });
  const params = new URLSearchParams(window.location.search);
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [org, setOrg] = useState("");
  const [memoryRepo, setMemoryRepo] = useState("");
  const [codeRepo, setCodeRepo] = useState("");
  const [showOther, setShowOther] = useState(false);
  const s = status.data;
  useEffect(() => {
    if (!s || s.busy) return;
    setBaseUrl((v) => v || (s.configured ? s.base_url : window.location.origin));
    setCodeRepo((v) => v || s.code_repo || s.detected.checkout_repo || "");
    const login = s.claimer?.login ?? (s.identity?.kind !== "app" ? s.identity?.login : undefined);
    setMemoryRepo((v) => v || s.memory || (login ? `${login}/auto-maintainer-memory` : ""));
  }, [s]);
  const refresh = () => client.invalidateQueries({ queryKey: ["setup"] });
  const useToken = useAct((body: { mode: "gh" | "token"; token?: string }) => api.post<Person>("/api/setup/github", body), [["setup"]]);
  const app = useAct(
    (purpose: "identity" | "signin") => api.post<{ url: string; manifest: string }>("/api/setup/app", { purpose, base_url: baseUrl, org: org || null }),
    [],
  );
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
  const appPending = s.app?.purpose === "identity" && !owner && s.first_run;

  return shell(
    <>
      {params.get("app") === "identity" && (
        <div className="notice">
          <Icon name="check" size={14} /> {t("GitHub created the app. Install it on your repositories, then sign in with it below.")}
        </div>
      )}
      {params.get("app") === "signin" && (
        <div className="notice">
          <Icon name="check" size={14} /> {t("GitHub created the sign-in app. Everyone can now sign in with GitHub.")}
        </div>
      )}
      {params.get("app_error") && <div className="error">{params.get("app_error")}</div>}
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

      <Step n={1} title={t("Address")} done={!!baseUrl} hint={t("What browsers use to reach this service. GitHub sends you back here.")}>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://your-host:7777" style={{ width: "100%" }} />
      </Step>

      <Step n={2} title={t("Connect GitHub")} done={!!id && proven} hint={t("The account the service acts as: it reads pull requests and CI, and posts what maintainers approve.")}>
        {id && (
          <div className="row">
            {id.avatar_url ? <img className="avatar" src={id.avatar_url} alt="" /> : <Icon name="cpu" />}
            <strong>{id.login}</strong>
            <span className="muted small">
              {id.kind === "gh" ? t("through the GitHub CLI") : id.kind === "token" ? t("with a token") : t("a GitHub App")}
            </span>
            {id.warning && <span className="small" style={{ color: "var(--warn)" }}>{id.warning}</span>}
            {!showOther && (
              <button className="ghost sm right" onClick={() => setShowOther(true)}>
                {t("Change")}
              </button>
            )}
          </div>
        )}
        {appPending && (
          <div className="choice">
            <strong>
              {t("App created:")} {s.app?.slug}
            </strong>
            <span className="small muted">{t("Install it on the repositories it should maintain, then sign in through it to prove you are a maintainer.")}</span>
            <div className="row">
              <a className="button" href={s.app?.install_url} target="_blank" rel="noreferrer">
                {t("Install the app")} <Icon name="external" size={12} />
              </a>
              <a className="button primary" href="/setup/signin">
                {t("Sign in with GitHub")}
              </a>
            </div>
          </div>
        )}
        {(!id || showOther) && !appPending && (
          <div className="choices">
            {s.detected.gh.installed && <GhWebLogin onDone={() => (setShowOther(false), void refresh())} />}
            <Choice
              icon="merge"
              title={t("Create a GitHub App")}
              recommended={!s.detected.gh.installed}
              body={t("Best for a team: the service gets its own bot account, and everyone signs in with GitHub. GitHub creates it in one click.")}
              action={
                <div className="row">
                  <input value={org} onChange={(e) => setOrg(e.target.value)} placeholder={t("organization (empty: your account)")} style={{ flex: 1, minWidth: 160 }} />
                  <button className="sm" disabled={!baseUrl || app.isPending} onClick={() => app.mutate("identity", { onSuccess: (r) => postToGitHub(r.url, r.manifest) })}>
                    {t("Create on GitHub")} <Icon name="external" size={12} />
                  </button>
                </div>
              }
            />
            <Choice
              icon="key"
              title={t("Use a token")}
              body={
                <>
                  {t("Create a token on GitHub with the repo scope and paste it here.")}{" "}
                  <a href={s.token_url} target="_blank" rel="noreferrer">
                    {t("Create a token")} <Icon name="external" size={11} />
                  </a>
                </>
              }
              action={
                <form
                  className="row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    useToken.mutate({ mode: "token", token }, { onSuccess: () => (setToken(""), setShowOther(false)) });
                  }}
                >
                  <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="ghp_… / github_pat_…" style={{ flex: 1, minWidth: 160 }} />
                  <button className="sm" disabled={!token.trim() || useToken.isPending}>
                    {t("Use token")}
                  </button>
                </form>
              }
            />
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
                  <button className="sm" disabled={useToken.isPending} onClick={() => useToken.mutate({ mode: "gh" }, { onSuccess: () => setShowOther(false) })}>
                    {t("Use")} {ghUser.login}
                  </button>
                }
              />
            )}
          </div>
        )}
        <ErrorBox error={useToken.error || app.error} />
      </Step>

      <Step n={3} title={t("Who can sign in")} done={!!id} hint={t("Anyone who signs in can only do what their GitHub permission on each repository allows.")}>
        {s.signin === "app" || s.auth === "app" ? (
          <span className="row small">
            <Status value="executed" label={t("GitHub sign-in")} />
            {t("Everyone signs in with their GitHub account through the app.")}
          </span>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            <span className="row small">
              <Status value="executed" label={t("GitHub sign-in")} />
              {t("Everyone signs in by entering a one-time code on github.com.")}
            </span>
            <span className="small muted row">
              {t("For a team, a sign-in app makes it one click:")}
              <button className="sm" disabled={!baseUrl || app.isPending || !proven} onClick={() => app.mutate("signin", { onSuccess: (r) => postToGitHub(r.url, r.manifest) })}>
                {t("Create a sign-in app")} <Icon name="external" size={12} />
              </button>
            </span>
          </div>
        )}
      </Step>

      <Step n={4} title={t("Memory repository")} done={!!s.memory || memory.data?.repo === null} hint={t("A private repository where the service keeps profiles, settings, and what it learns, as reviewable commits.")}>
        {s.memory && <Status value="executed" label={s.memory} />}
        <div className="row">
          <input value={memoryRepo} onChange={(e) => setMemoryRepo(e.target.value)} placeholder="owner/auto-maintainer-memory" style={{ flex: 1, minWidth: 220 }} />
          {s.auth === "app" && memoryRepo.includes("/") && (
            <a
              className="button"
              href={`https://github.com/new?owner=${encodeURIComponent(memoryRepo.split("/")[0])}&name=${encodeURIComponent(memoryRepo.split("/")[1])}&visibility=private`}
              target="_blank"
              rel="noreferrer"
            >
              {t("Create it on GitHub")} <Icon name="external" size={12} />
            </a>
          )}
          <button className="primary sm" disabled={!memoryRepo || memory.isPending || !proven || !id} onClick={() => memory.mutate({ repo: memoryRepo, create: true })}>
            {s.auth === "app" ? t("Use it") : t("Create or use")}
          </button>
          <button className="ghost sm" disabled={memory.isPending || !proven} onClick={() => memory.mutate({ repo: null, create: false })}>
            {t("Keep it on this host")}
          </button>
        </div>
        {s.auth === "app" && <span className="small muted">{t("Then install the app on it too, so the service can write there.")}</span>}
        {memory.data && (
          <span className="small muted">
            {memory.data.repo ? (memory.data.created ? t("Created a private repository.") : t("Using the private repository.")) : t("Memory stays on this host.")}
          </span>
        )}
        <ErrorBox error={memory.error} />
      </Step>

      <Step n={5} title={t("The service's own repository")} done={!!codeRepo} optional hint={t("Its maintainers own the service settings and approve the changes the service makes to itself.")}>
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
          disabled={!baseUrl || !(id || (s.configured && !s.first_run)) || !proven || finish.isPending}
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
  const [code, setCode] = useState<{ code: string; url: string; flow_id?: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );
  const poll = (flowId?: string) => {
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.post<{ state?: string; pending?: boolean; error?: string; login?: string }>(pollPath, flowId ? { flow_id: flowId } : {});
        if (r.state === "pending" || r.pending) return poll(flowId);
        if (r.state === "done" || r.login) {
          setCode(null);
          onDone();
          return;
        }
        setCode(null);
        setError(new Error(r.error || t("The sign-in did not finish; try again.")));
      } catch (e) {
        if (e instanceof ApiError && e.status === 202) return poll(flowId);
        setError(e);
        setCode(null);
      }
    }, 2000);
  };
  const start = async () => {
    setError(null);
    try {
      const r = await api.post<{ code: string; url: string; flow_id?: string }>(startPath);
      setCode(r);
      poll(r.flow_id);
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Choice
      icon="globe"
      title={title ?? t("Sign in with GitHub")}
      recommended
      body={body ?? t("You enter a one-time code on github.com and the service works as your account. Simplest for one maintainer.")}
      action={
        <div className="stack" style={{ gap: 8 }}>
          {code ? (
            <>
              <div className="row" style={{ gap: 8 }}>
                <span className="code" style={{ fontSize: 22 }}>
                  {code.code}
                </span>
                <button
                  className="ghost sm"
                  onClick={() => {
                    void navigator.clipboard?.writeText(code.code);
                    setCopied(true);
                  }}
                >
                  {copied ? t("Copied") : t("Copy")}
                </button>
              </div>
              <a className="button primary" href={code.url} target="_blank" rel="noreferrer">
                {t("Enter it on GitHub")} <Icon name="external" size={12} />
              </a>
              <span className="small muted row" style={{ gap: 6 }}>
                <span className="typing" aria-hidden>
                  <span />
                  <span />
                  <span />
                </span>
                {t("Waiting for you to authorize on GitHub…")}
              </span>
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
