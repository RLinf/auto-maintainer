// Host setup in the browser: the address, the service's repository (its owner's fork
// or copy of auto-maintainer) and with it the main forge, GitHub or GitLab, signing in
// there as an admin of that repository (who owns this service), the account the
// service acts as, the checks that let it change its own code later, the memory
// repository, and the agent that runs tasks first. On a new host, only an admin of the
// repository this checkout came from may claim it without the setup link printed in
// the terminal; later, the owner reaches this page from Settings.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import { copyText } from "../clipboard";
import { ErrorBox, Status, useAct } from "../components";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { Logo } from "../logo";
import { Skeleton } from "../ui";

interface Person {
  login: string;
  name: string;
  avatar_url: string;
  warning?: string;
}

interface Forge {
  id: string;
  kind: "github" | "gitlab";
  web_url: string;
  /** Whether its CLI can sign in with a one-time code. */
  device: boolean;
}

interface Check {
  id: string;
  ok: boolean;
  text: string;
}

interface SetupStatus {
  first_run: boolean;
  mine: boolean;
  busy?: string;
  code: boolean;
  /** Whether signing in as an admin of the repository makes the caller its owner. */
  may_claim: boolean;
  configured: boolean;
  base_url: string;
  forge: Forge;
  repo: string | null;
  repo_link: string | null;
  owner: string | null;
  identity: Person | null;
  claimer: Person | null;
  /** Signed in as the owner, but not yet an admin of the repository named. */
  candidate: Person | null;
  checks: Check[];
  /** How the host reached GitHub before only the GitHub CLI remained, if it must connect again. */
  reconnect: string | null;
  memory: string | null;
  /** The agent chosen at the last setup, if any. */
  default_agent: string | null;
  detected: {
    cli: { name: string; installed: boolean; user?: Person; error?: string };
    agents: Record<string, boolean>;
    checkout: { web_url: string; path: string; link: string; kind?: string } | null;
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

const KNOWN_HOSTS: Record<string, "github" | "gitlab"> = { "github.com": "github", "gitlab.com": "gitlab" };

/** The host of a repository link, for guessing its forge. */
function hostOf(link: string): string {
  const m = link.trim().match(/^(?:[\w.-]+@([\w.-]+):|(?:https?|ssh|git):\/\/(?:[^@/]+@)?([\w.-]+))/);
  return (m?.[1] ?? m?.[2] ?? "").toLowerCase();
}

function siteName(forge: Forge): string {
  const name = forge.kind === "gitlab" ? "GitLab" : "GitHub";
  const host = forge.web_url.replace(/^https?:\/\//, "");
  return host === "github.com" || host === "gitlab.com" ? name : `${name} (${host})`;
}

/** The agent preselected on the page: the one chosen before, else the first installed. */
function preferredAgent(s: SetupStatus): string | null {
  if (s.default_agent) return s.default_agent;
  return Object.keys(AGENT_NAMES).find((name) => s.detected.agents[name]) ?? null;
}

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
  const [link, setLink] = useState("");
  const [kind, setKind] = useState<"github" | "gitlab" | "">("");
  const [clientId, setClientId] = useState("");
  const [moreRepo, setMoreRepo] = useState(false);
  const [otherAccount, setOtherAccount] = useState(false);
  const [agent, setAgent] = useState<string | null | undefined>(undefined);
  const s = status.data;
  useEffect(() => {
    if (!s || s.busy) return;
    setBaseUrl((v) => v || (s.configured ? s.base_url : window.location.origin));
    setLink((v) => v || s.repo_link || s.detected.checkout?.link || "");
    const login = s.claimer?.login ?? s.identity?.login;
    setMemoryRepo((v) => v || s.memory || (login ? `${login}/auto-maintainer-memory` : ""));
    setAgent((v) => (v === undefined ? preferredAgent(s) : v));
  }, [s]);
  const refresh = () => client.invalidateQueries({ queryKey: ["setup"] });
  const useRepo = useAct(
    () => api.post<{ repo: string; forge: Forge }>("/api/setup/repo", { link, kind: kind || null, oauth_client_id: clientId || null }),
    [["setup"]],
  );
  const copy = useAct(() => api.post<{ repo: string }>("/api/setup/copy"), [["setup"]]);
  const check = useAct(() => api.post<{ checks: Check[] }>("/api/setup/check"), [["setup"]]);
  const hostLogin = useAct(() => api.post<Person>("/api/setup/host-login"), [["setup"]]);
  const memory = useAct((body: { repo: string | null; create: boolean }) => api.post<{ repo: string | null; created?: boolean }>("/api/setup/memory", body), [["setup"]]);
  const finish = useAct(() => api.post<{ redirect: string }>("/api/setup/finish", { base_url: baseUrl, default_agent: agent ?? null }), []);

  const shell = (children: ReactNode) => (
    <div style={firstRun ? { minHeight: "100vh", display: "flex", justifyContent: "center", padding: "48px 16px 96px" } : { display: "flex", justifyContent: "center" }}>
      <div style={{ width: "min(760px, 100%)", display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="row" style={{ gap: 12 }}>
          <Logo size={44} />
          <div className="stack" style={{ gap: 2 }}>
            <h1>{firstRun ? t("Set up auto-maintainer") : t("Host setup")}</h1>
            <span className="muted">
              {firstRun
                ? t("Everything happens here and on your forge, GitHub or GitLab. Nothing to type in a terminal, no files to edit.")
                : t("Change how this host connects to its forge. The service restarts on the new setup when you save.")}
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
        <span className="muted">{t("Only they can finish it. When they are done, sign in here.")}</span>
      </div>,
    );
  }

  const forge = s.forge;
  const site = siteName(forge);
  const owner = s.claimer;
  const checkout = s.detected.checkout;
  const linkHost = hostOf(link);
  const unknownHost = !!linkHost && !KNOWN_HOSTS[linkHost];
  const linkKind = kind || KNOWN_HOSTS[linkHost] || "";
  const savedLink = (s.repo_link ?? "").toLowerCase() === link.trim().replace(/\.git$/, "").replace(/\/+$/, "").toLowerCase();
  const checksOk = s.checks.length > 0 && s.checks.every((c) => c.ok);
  const repoChanged = (s.repo ?? "") !== "" && s.configured && !s.first_run && !!s.claimer;
  const needOwner = s.first_run || repoChanged || !!s.code;
  const ready = !!baseUrl && (!!s.identity || (s.configured && !s.first_run && !s.reconnect)) && (!needOwner || (!!owner && checksOk));
  const canCopy = !!checkout && !!s.repo && !owner && (!!s.candidate || s.checks.some((c) => c.id === "code" && !c.ok));
  const cliMissing = !s.detected.cli.installed;

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
            {t("Signed in as")} <strong>{owner.login}</strong> · {t("an admin of")} <code>{s.repo}</code>, {t("who owns this service")}
          </span>
        </div>
      )}
      {s.first_run && !owner && !s.code && (
        <div className="notice small">
          <Icon name="shield" size={13} />{" "}
          {checkout
            ? `${t("Without the setup link printed when the service started, only an admin of")} ${checkout.path} ${t("can set this host up; you prove it by signing in below.")}`
            : t("This checkout does not say where its code came from. Open the setup link printed when the service started.")}
        </div>
      )}

      <Step n={1} title={t("Address")} done={!!baseUrl} hint={t("What browsers use to reach this service; links in alerts point here.")}>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://your-host" style={{ width: "100%" }} />
      </Step>

      <Step
        n={2}
        title={t("Your auto-maintainer repository")}
        done={!!s.repo && savedLink}
        hint={t("Your own fork or copy of auto-maintainer. Its admin owns this service, updates are deployed from it, and the changes the service makes to itself are proposed there. Its forge, GitHub or GitLab, is where this service works.")}
      >
        <div className="row" style={{ gap: 8 }}>
          <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://github.com/you/auto-maintainer" style={{ flex: 1, minWidth: 240 }} spellCheck={false} />
          {unknownHost && (
            <select value={kind} onChange={(e) => setKind(e.target.value as "github" | "gitlab" | "")} aria-label={t("Kind")}>
              <option value="">{t("GitHub or GitLab?")}</option>
              <option value="github">GitHub</option>
              <option value="gitlab">GitLab</option>
            </select>
          )}
          <button className={savedLink ? "sm" : "primary sm"} disabled={!link.trim() || (unknownHost && !kind) || useRepo.isPending} onClick={() => useRepo.mutate(undefined)}>
            {t("Use it")}
          </button>
        </div>
        {linkKind === "gitlab" && unknownHost && (
          <button type="button" className="link-button small" style={{ alignSelf: "flex-start", color: "var(--info)" }} onClick={() => setMoreRepo(!moreRepo)}>
            <Icon name={moreRepo ? "chevronDown" : "chevronRight"} size={13} />
            {t("More options")}
          </button>
        )}
        {moreRepo && linkKind === "gitlab" && unknownHost && (
          <label className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("OAuth application client id (self-managed GitLab, for the one-time code)")}</span>
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} spellCheck={false} />
          </label>
        )}
        {s.repo && (
          <span className="row small" style={{ gap: 6 }}>
            <Status value="executed" label={s.repo} />
            <span className="pill">{site}</span>
            <span className="muted">{s.first_run ? t("the forge this service works on") : t("this host's forge")}</span>
          </span>
        )}
        {canCopy && checkout && (
          <Choice
            icon="merge"
            title={`${t("Make your own copy of")} ${checkout.path}`}
            body={t("The service makes a fork, or an import on another forge, under the account you signed in with, and uses it as this service's repository. You own it as its admin.")}
            action={
              <button className="primary sm" disabled={copy.isPending} onClick={() => copy.mutate(undefined, { onSuccess: (r) => setLink(`${forge.web_url}/${r.repo}`) })}>
                {copy.isPending ? t("Copying…") : t("Make my copy")}
              </button>
            }
          />
        )}
        <ErrorBox error={useRepo.error || copy.error} />
      </Step>

      <Step
        n={3}
        title={`${t("Sign in on")} ${site}`}
        done={!!owner || (!needOwner && !!s.identity)}
        hint={t("Sign in as an admin of the repository: you become the owner of this service, and the service acts as your account there unless you choose another one below.")}
      >
        {!s.repo ? (
          <span className="small muted">{t("Name the repository first.")}</span>
        ) : cliMissing ? (
          <CliInstall name={s.detected.cli.name} site={site} onDone={() => void refresh()} />
        ) : (
          <>
            {s.candidate && !owner && (
              <div className="notice small">
                <strong>{s.candidate.login}</strong> {t("signed in, but is not an admin of")} <code>{s.repo}</code>. {t("Make your own copy above, or name a repository you are an admin of.")}
              </div>
            )}
            {!owner && <ForgeSignIn role="owner" forge={forge} site={site} onDone={() => void refresh()} />}
            {s.identity && (
              <div className="row">
                {s.identity.avatar_url ? <img className="avatar" src={s.identity.avatar_url} alt="" /> : <Icon name="cpu" />}
                <span className="small">
                  {t("The service acts as")} <strong>{s.identity.login}</strong>
                </span>
                {s.identity.warning && <span className="small" style={{ color: "var(--warn)" }}>{s.identity.warning}</span>}
                {!otherAccount && (owner || s.code || !s.first_run) && (
                  <button className="ghost sm right" onClick={() => setOtherAccount(true)}>
                    {t("Use another account")}
                  </button>
                )}
              </div>
            )}
            {otherAccount && (
              <div className="choices">
                <ForgeSignIn
                  role="service"
                  forge={forge}
                  site={site}
                  title={t("An account made for the service")}
                  body={t("For a team, sign in as an account made for the service, so what it posts is not under your name. It needs write access to the repository.")}
                  onDone={() => (setOtherAccount(false), void refresh())}
                />
                {(s.code || !s.first_run) && s.detected.cli.user && (
                  <Choice
                    icon="cpu"
                    title={t("The CLI login already on this host")}
                    body={
                      <>
                        {t("Logged in as")} <strong>{s.detected.cli.user.login}</strong>.
                      </>
                    }
                    action={
                      <button className="sm" disabled={hostLogin.isPending} onClick={() => hostLogin.mutate(undefined, { onSuccess: () => setOtherAccount(false) })}>
                        {t("Use")} {s.detected.cli.user.login}
                      </button>
                    }
                  />
                )}
              </div>
            )}
            <ErrorBox error={hostLogin.error} />
          </>
        )}
      </Step>

      <Step n={4} title={t("Checks")} done={checksOk} hint={t("So that the service can update itself and propose changes to its own code later.")}>
        {s.checks.length === 0 ? (
          <span className="small muted">{t("They run once you have signed in.")}</span>
        ) : (
          <div className="stack" style={{ gap: 6 }}>
            {s.checks.map((c) => (
              <span key={c.id} className="row small" style={{ gap: 8, alignItems: "flex-start", flexWrap: "nowrap" }}>
                <span style={{ color: c.ok ? "var(--ok)" : "var(--bad)", flex: "none", marginTop: 2 }}>
                  <Icon name={c.ok ? "check" : "x"} size={14} />
                </span>
                <span>{c.text}</span>
              </span>
            ))}
            {!checksOk && (
              <button className="sm" style={{ alignSelf: "flex-start" }} disabled={check.isPending} onClick={() => check.mutate(undefined)}>
                {t("Check again")}
              </button>
            )}
          </div>
        )}
        <ErrorBox error={check.error} />
      </Step>

      <Step n={5} title={t("Memory repository")} done={!!s.memory || memory.data?.repo === null} hint={t("A private repository where the service keeps profiles, settings, and what it learns, as reviewable commits.")}>
        {s.memory && <Status value="executed" label={s.memory} />}
        <div className="row">
          <input value={memoryRepo} onChange={(e) => setMemoryRepo(e.target.value)} placeholder="you/auto-maintainer-memory" style={{ flex: 1, minWidth: 220 }} />
          <button className="primary sm" disabled={!memoryRepo || memory.isPending || !s.identity} onClick={() => memory.mutate({ repo: memoryRepo, create: true })}>
            {t("Create or use")}
          </button>
          <button className="ghost sm" disabled={memory.isPending || !s.identity} onClick={() => memory.mutate({ repo: null, create: false })}>
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

      <Step
        n={6}
        title={t("The agent that does the work")}
        done={!!agent}
        hint={t("Every task runs on it first. When it cannot run, the other agents installed here take over. Model providers and per-task choices are on the Settings page.")}
      >
        <div className="agent-options" role="radiogroup" aria-label={t("Default agent")}>
          {Object.entries(AGENT_NAMES).map(([name, label]) => {
            const found = !!s.detected.agents[name];
            return (
              <label key={name} className={`agent-option ${agent === name ? "on" : ""} ${found ? "" : "missing"}`}>
                <input type="radio" name="default-agent" checked={agent === name} disabled={!found && agent !== name} onChange={() => setAgent(name)} />
                <strong>{label}</strong>
                <span className="small muted right">{found ? t("installed") : t("not installed")}</span>
              </label>
            );
          })}
        </div>
        {!Object.values(s.detected.agents).some(Boolean) && (
          <div className="notice small">
            {t("No agent CLI is installed on this host. Install one, such as Claude Code, Codex, or Antigravity, and sign it in; the service uses it as soon as it is installed.")}
          </div>
        )}
        {agent && !s.detected.agents[agent] && (
          <div className="notice small">{t("This agent is not installed on this host any more; pick one that is.")}</div>
        )}
      </Step>

      <div className="row" style={{ justifyContent: "flex-end" }}>
        <ErrorBox error={finish.error} />
        <button
          className="primary"
          style={{ height: 40, padding: "0 18px" }}
          disabled={!ready || finish.isPending}
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

/** Sign in on the main forge for the setup: the forge CLI's one-time code where it
 * can, and on GitLab a personal access token too. */
function ForgeSignIn({ role, forge, site, title, body, onDone }: { role: "owner" | "service"; forge: Forge; site: string; title?: string; body?: string; onDone: () => void }) {
  const t = useT();
  const [token, setToken] = useState("");
  const withToken = useMutation({
    mutationFn: () => api.post<{ state: string; error?: string }>("/api/setup/signin/token", { role, token }),
    onSuccess: (r) => {
      if (r.state === "done") {
        setToken("");
        onDone();
      }
    },
  });
  const failed = withToken.data?.state === "failed" ? new Error(withToken.data.error ?? t("The sign-in did not finish; try again.")) : null;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {forge.device && (
        <GhWebLogin
          startBody={{ role }}
          site={site}
          title={title ?? `${t("Sign in with")} ${site}`}
          body={body ?? t("You enter a one-time code on the forge's site and authorize its CLI as yourself, an admin of the repository.")}
          button={`${t("Sign in with")} ${site}`}
          onDone={onDone}
        />
      )}
      {forge.kind === "gitlab" && (
        <Choice
          icon="key"
          title={t("With a personal access token")}
          body={forge.device ? t("Or paste a token with the scopes api and read_user.") : t("This GitLab needs an OAuth application for the one-time code; paste a token with the scopes api and read_user instead.")}
          action={
            <form
              className="row"
              style={{ gap: 8 }}
              onSubmit={(e) => {
                e.preventDefault();
                withToken.mutate();
              }}
            >
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" style={{ flex: 1, minWidth: 200 }} />
              <button className="primary sm" disabled={!token.trim() || withToken.isPending}>
                {withToken.isPending ? t("Signing in…") : t("Sign in")}
              </button>
            </form>
          }
        />
      )}
      <ErrorBox error={withToken.error || failed} />
    </div>
  );
}

/** Install the main forge's CLI on a host that has none, from the forge's own release
 * into the service's data folder. */
function CliInstall({ name, site, onDone }: { name: string; site: string; onDone: () => void }) {
  const t = useT();
  const install = useMutation({ mutationFn: () => api.post<{ installed: boolean }>("/api/setup/cli/install"), onSuccess: onDone });
  return (
    <Choice
      icon="download"
      title={`${t("Install")} ${name}`}
      body={`${t("The service reaches")} ${site} ${t("through its CLI, which this host does not have yet. Installing downloads the CLI's own release into the service's data folder and checks it against the release's checksums; nothing else on the host changes.")}`}
      action={
        <div className="stack" style={{ gap: 8 }}>
          <button className="primary sm" disabled={install.isPending} onClick={() => install.mutate()}>
            <Icon name="download" size={14} />
            {install.isPending ? t("Installing…") : `${t("Install")} ${name}`}
          </button>
          <ErrorBox error={install.error} />
        </div>
      }
    />
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

/** Log a forge's CLI in from the browser: show the forge's one-time code and wait
 * until the person has entered it on the forge's site (GitHub by default). */
export function GhWebLogin({
  onDone,
  start: startPath = "/api/setup/signin/start",
  poll: pollPath = "/api/setup/signin/poll",
  startBody,
  site = "GitHub",
  title,
  body,
  button,
}: {
  onDone: () => void;
  start?: string;
  poll?: string;
  /** Sent with the start request, such as whom the sign-in is for. */
  startBody?: unknown;
  /** The forge's name, as people know it. */
  site?: string;
  title?: string;
  body?: string;
  button?: string;
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
        const r = await api.post<{ state?: string; pending?: boolean; error?: string; login?: string }>(pollPath, flowId ? { flow_id: flowId } : undefined);
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
      const r = await api.post<{ code: string; url: string; flow_id?: string }>(startPath, startBody);
      const flow = { ...r, exp: Date.now() + FLOW_MS };
      saveFlow(pollPath, flow);
      resumed.current = false;
      setCode(flow);
      poll(r.flow_id);
    } catch (e) {
      setError(e);
    }
  };
  const [copyFailed, setCopyFailed] = useState(false);
  const copy = () => {
    if (!code) return;
    void copyText(code.code).then((ok) => {
      setCopied(ok);
      setCopyFailed(!ok);
    });
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
                <span className="code" style={{ fontSize: 22, userSelect: "all" }} title={t("Click to select the code")}>
                  {code.code}
                </span>
                <button className="ghost sm" onClick={copy}>
                  {copied ? t("Copied") : t("Copy")}
                </button>
              </div>
              {copyFailed && <span className="small" style={{ color: "var(--warn)" }}>{t("This browser would not copy it. Click the code to select it, then press Ctrl+C (⌘C on a Mac).")}</span>}
              <a className="button primary" href={code.url} target="_blank" rel="noopener noreferrer" onClick={copy}>
                {site === "GitHub" ? t("Copy the code and open GitHub") : `${t("Copy the code and open")} ${site}`} <Icon name="external" size={12} />
              </a>
              <span className="small muted row" style={{ gap: 6 }}>
                <span className="typing" aria-hidden>
                  <span />
                  <span />
                  <span />
                </span>
                {site === "GitHub"
                  ? t("Waiting for you to authorize on GitHub. Paste the code there; this page keeps it, and finishes by itself once you authorize, even if you come back to it later.")
                  : t("Waiting for you to authorize there. Paste the code there; this page keeps it, and finishes by itself once you authorize, even if you come back to it later.")}
              </span>
              <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => void start()}>
                {t("Get a new code")}
              </button>
            </>
          ) : (
            <button className="primary sm" onClick={() => void start()}>
              {button ?? t("Sign in with GitHub")}
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
