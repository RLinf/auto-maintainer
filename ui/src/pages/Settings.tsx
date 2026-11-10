// Settings: how the service runs. Agents, model providers, default routes, and system
// values in one place, each row showing its current value. Changes are made in place
// (an agent's model, a provider's form), by asking the agent (it drafts the edit, you
// apply it), or by editing the settings file; API keys go only into their key fields.
// Only the owner of the service applies changes; anyone else's wait for the owner. The
// Agents and Model providers tabs also hold the signed-in person's own agents and
// model APIs, which are theirs alone.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type AgentChoices, type AgentInstall, type LabMachine, type MachineCheck, type MachineDraft, type SettingsAgent, type SettingsCheck, type SandboxView, type SettingsProvider, type SettingsView } from "../api";
import { useAgent, type AgentContext } from "../agent";
import { Diff, Empty, ErrorBox, PageHead, Section, Status, useAct, When, GrowingTextarea } from "../components";
import { useRepos, useSettings } from "../data";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { agentName } from "../agentPicker";
import { useAgentsData } from "./Agents";
import { SettingsChangeCard } from "../settingsChange";
import { MyAgentsSection, MyApisSection, ServiceSignIn } from "../agentAccounts";
import { ApiForm, ApiSummary, modelsFor, PROTOCOLS } from "../providerForm";
import { ForgesCard } from "../forgeSetup";
import { OwnerCard } from "../ownerCard";
import { Popover, Skeleton, Tabs, useTab, useToast } from "../ui";

const CONTEXT: AgentContext = { page: "settings", repo: null, number: null, label: "Settings" };

const IDEAS = [
  "Run Claude Code on DeepSeek",
  "Add an OpenAI-compatible provider",
  "Review with Codex, fall back to Claude Code",
  "Cap Claude Code at $50 a day",
  "Check repositories every 5 minutes",
];

const TASKS: Record<string, string> = {
  triage: "Labels and summarizes new PRs",
  review: "Reviews a PR",
  verify: "Double-checks a review's findings",
  followup: "Re-reviews after new commits",
  evaluate: "Scores priority",
  calibrate: "Learns from priority overrides",
  ci_diagnose: "Explains failed CI jobs",
  ci_root_cause: "Finds why real CI failures happen, in the code",
  hygiene: "Checks PR conventions",
  onboard: "Drafts a new repository's profile",
  learn: "Turns feedback into lessons",
  chat: "Answers in conversations",
  self_improve: "Changes the service's own code",
  probe: "Health checks",
  translate: "Translates reviews for reading on the dashboard",
};

export default function SettingsPage() {
  const t = useT();
  const agent = useAgent();
  const view = useSettings();
  const tabs = [
    { id: "agents", label: t("Agents"), count: view.data?.agents.filter((a) => a.enabled).length },
    { id: "providers", label: t("Model providers"), count: view.data?.providers.length },
    { id: "routing", label: t("Task routing") },
    { id: "labs", label: t("Test machines"), count: view.data?.labs.length },
    { id: "system", label: t("System") },
    { id: "file", label: t("Settings file") },
  ];
  const [tab, setTab] = useTab(tabs);
  const ask = (prompt: string, send = false) => agent.open({ context: { ...CONTEXT, label: t("Settings") }, prompt, send });

  if (view.isLoading) {
    return (
      <div className="card">
        <Skeleton rows={8} />
      </div>
    );
  }
  if (view.error || !view.data) return <ErrorBox error={view.error} />;
  const v = view.data;
  const missingKeys = v.providers.filter((p) => p.api_key && !p.key?.configured);

  return (
    <>
      <PageHead
        title={t("Settings")}
        sub={
          <span className="row" style={{ gap: 6 }}>
            {v.access.allowed ? (
              <>
                <Icon name="shield" size={14} />
                {t("You can change these settings")}: {v.access.actual} {t("on")} <code>{v.repo}</code>
              </>
            ) : (
              <>
                <Icon name="eye" size={14} />
                {v.repo
                  ? `${t("View only: changing them needs")} ${v.access.required} ${t("on")} ${v.repo}. ${t("What you ask for waits for a maintainer.")}`
                  : v.access.reason}
              </>
            )}
          </span>
        }
      />

      <AskCard onAsk={ask} />

      {!v.status.applied && (
        <div className="strip paused" role="alert">
          <Icon name="x" />
          <span>
            {t("The settings file in the memory repository does not apply; the previous settings stay in force.")} {v.status.problems.join("; ")}
          </span>
          <button className="sm right" onClick={() => ask(`${t("The settings file does not apply")}: ${v.status.problems.join("; ")}. ${t("Fix it.")}`, true)}>
            <Icon name="sparkles" size={14} />
            {t("Fix with the agent")}
          </button>
        </div>
      )}
      {missingKeys.length > 0 && (
        <div className="strip question">
          <Icon name="key" />
          <span>
            {missingKeys.map((p) => p.api_key).join(", ")} {t("not set yet: agents that use")} {missingKeys.map((p) => p.name || p.id).join(", ")}{" "}
            {t("cannot run until it is.")}
          </span>
          <button className="sm right" onClick={() => setTab("providers")}>
            {t("Set key")}
          </button>
        </div>
      )}

      {v.pending.length > 0 && (
        <section className="stack">
          <h2>
            {t("Waiting to be applied")} <span className="muted">· {v.pending.length}</span>
          </h2>
          {v.pending.map((c) => (
            <SettingsChangeCard key={c.id} change={c} />
          ))}
        </section>
      )}

      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      {tab === "agents" && (
        <div className="stack" style={{ gap: 16 }}>
          <AgentsTab v={v} ask={ask} />
          <MyAgentsSection />
          <SandboxCard />
        </div>
      )}
      {tab === "providers" && (
        <div className="stack" style={{ gap: 16 }}>
          <ProvidersTab v={v} ask={ask} />
          <MyApisSection />
        </div>
      )}
      {tab === "routing" && <RoutingTab v={v} ask={ask} />}
      {tab === "labs" && <LabsTab v={v} ask={ask} />}
      {tab === "system" && (
        <div className="stack" style={{ gap: 16 }}>
          <SystemTab v={v} ask={ask} />
          <OwnerCard />
          <ForgesCard canChange={v.access.allowed} />
        </div>
      )}
      {tab === "file" && <FileTab v={v} />}
    </>
  );
}

function AskCard({ onAsk }: { onAsk: (prompt: string, send?: boolean) => void }) {
  const t = useT();
  const [text, setText] = useState("");
  return (
    <section className="card" style={{ background: "linear-gradient(135deg, var(--accent-soft), var(--surface) 70%)" }}>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="avatar-ai" style={{ width: 30, height: 30, borderRadius: 10, display: "grid", placeItems: "center", color: "#fff", background: "linear-gradient(135deg, var(--accent), var(--accent-2))" }}>
          <Icon name="sparkles" size={16} />
        </span>
        <div className="stack" style={{ gap: 0 }}>
          <strong>{t("Change settings by asking")}</strong>
          <span className="small muted">{t("Say what you want. The agent drafts the change; you see the diff and apply it.")}</span>
        </div>
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) {
            onAsk(text.trim(), true);
            setText("");
          }
        }}
      >
        <GrowingTextarea
          value={text}
          placeholder={t("For example: use DeepSeek for Claude Code, and let Codex verify reviews")}
          aria-label={t("What should change?")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (text.trim()) {
                onAsk(text.trim(), true);
                setText("");
              }
            }
          }}
        />
        <button className="primary icon" disabled={!text.trim()} aria-label={t("Ask")}>
          <Icon name="send" size={15} />
        </button>
      </form>
      <div className="suggestions" style={{ marginTop: 10 }}>
        {IDEAS.map((idea) => (
          <button key={idea} className="suggestion" onClick={() => onAsk(t(idea))}>
            {t(idea)}
          </button>
        ))}
      </div>
    </section>
  );
}

function Row({ icon, title, sub, value, actions }: { icon?: IconName; title: ReactNode; sub?: ReactNode; value?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="list-row" style={{ padding: "12px 4px", alignItems: "flex-start" }}>
      {icon && (
        <span style={{ paddingTop: 2 }}>
          <Icon name={icon} />
        </span>
      )}
      <span className="main">
        <span className="t" style={{ whiteSpace: "normal" }}>
          {title}
        </span>
        {sub && (
          <span className="small muted" style={{ display: "block" }}>
            {sub}
          </span>
        )}
      </span>
      {value && <span className="small" style={{ textAlign: "right", maxWidth: "45%" }}>{value}</span>}
      {actions && <span className="row" style={{ gap: 4, flexWrap: "nowrap" }}>{actions}</span>}
    </div>
  );
}

/** What skipping permission checks means for each CLI. */
const SKIP_FLAG: Record<string, string> = {
  "claude-code": "--dangerously-skip-permissions",
  antigravity: "--dangerously-skip-permissions",
  codex: "sandbox danger-full-access, approvals never",
  gemini: "--approval-mode yolo",
  opencode: "every permission set to allow",
};

/** Turn an agent's own skip-permissions mode on or off: a settings change, applied at
 * once for the owner of the service's repository and proposed otherwise. */
/** What an agent runs on when a route names nothing else: its CLI's own sign-in or a
 * model provider that serves a model it can run, which model, and a reasoning level
 * that model takes (the provider's tested levels, else what the CLI offers). */
function ModelControls({
  agent,
  choices,
  providers,
  canChange,
}: {
  agent: SettingsAgent;
  choices: AgentChoices | null | undefined;
  providers: SettingsProvider[];
  canChange: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const set = useAct(
    (body: { model: string | null; effort: string | null; provider?: string | null }) => api.put<{ approved: boolean }>(`/api/settings/agents/${agent.name}/model`, body),
    [["settings"], ["memory"]],
  );
  const done = (r: { approved: boolean }) => toast({ title: r.approved ? t("Saved") : t("Change proposed"), icon: r.approved ? "check" : "inbox" });
  const usable = providers.filter((p) => modelsFor(p, agent.adapter).length > 0 || p.id === agent.provider);
  const provider = providers.find((p) => p.id === agent.provider);
  const providerModels = provider ? modelsFor(provider, agent.adapter) : [];
  const cliModels = choices?.models ?? [];
  const nameOf = (id: string | null | undefined) => (id ? cliModels.find((m) => m.id === id)?.name || id : null);
  const cliModel = nameOf(choices?.default_model);
  const spec = provider?.models.find((m) => m.id === (agent.model ?? providerModels[0]?.id));
  const efforts = provider ? (spec?.reasoning.length ? spec.reasoning : []) : (choices?.efforts ?? []);
  const cliEffort = provider ? null : choices?.default_effort;
  const disabled = !canChange || set.isPending;
  const title = canChange ? undefined : t("Only the owner of this service changes agents.");
  return (
    <span className="row model-controls" style={{ gap: 6 }}>
      {(usable.length > 0 || agent.provider) && (
        <select
          value={agent.provider ?? ""}
          disabled={disabled}
          title={title ?? t("What the agent runs on")}
          aria-label={`${agent.name}: ${t("Runs on")}`}
          onChange={(e) => set.mutate({ provider: e.target.value || null, model: null, effort: null }, { onSuccess: done })}
        >
          <option value="">{t("its own sign-in")}</option>
          {usable.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name || p.id}
            </option>
          ))}
        </select>
      )}
      {provider ? (
        <select
          value={agent.model ?? ""}
          disabled={disabled}
          title={title}
          aria-label={`${agent.name}: ${t("Model")}`}
          onChange={(e) => set.mutate({ model: e.target.value || null, effort: null }, { onSuccess: done })}
        >
          <option value="">{providerModels[0] ? `${providerModels[0].id} (${t("the provider's first")})` : t("the provider's first")}</option>
          {providerModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
          {agent.model && !providerModels.some((m) => m.id === agent.model) && <option value={agent.model}>{agent.model}</option>}
        </select>
      ) : cliModels.length > 0 ? (
        <select
          value={agent.model ?? ""}
          disabled={disabled}
          title={title}
          aria-label={`${agent.name}: ${t("Model")}`}
          onChange={(e) => set.mutate({ model: e.target.value || null, effort: agent.effort }, { onSuccess: done })}
        >
          <option value="">{cliModel ? `${cliModel} (${t("the CLI's default")})` : t("the CLI's default")}</option>
          {cliModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name || m.id}
            </option>
          ))}
          {agent.model && !cliModels.some((m) => m.id === agent.model) && <option value={agent.model}>{agent.model}</option>}
        </select>
      ) : null}
      {efforts.length > 0 ? (
        <select
          value={agent.effort ?? ""}
          disabled={disabled}
          title={title}
          aria-label={`${agent.name}: ${t("Thinking")}`}
          onChange={(e) => set.mutate({ model: agent.model, effort: e.target.value || null }, { onSuccess: done })}
        >
          <option value="">{cliEffort ? `${t("Thinking")}: ${cliEffort} (${t("default")})` : `${t("Thinking")}: ${t("default")}`}</option>
          {efforts.map((e) => (
            <option key={e} value={e}>
              {t("Thinking")}: {e}
            </option>
          ))}
        </select>
      ) : null}
    </span>
  );
}

function AgentModeSwitch({ agent, canChange }: { agent: SettingsAgent; canChange: boolean }) {
  const t = useT();
  const toast = useToast();
  const set = useAct(
    (mode: "bypass" | null) => api.put<{ approved: boolean }>(`/api/settings/agents/${agent.name}/permissions`, { permissions: mode }),
    [["settings"], ["memory"]],
  );
  const done = (r: { approved: boolean }) => toast({ title: r.approved ? t("Saved") : t("Change proposed"), icon: r.approved ? "check" : "inbox" });
  if (agent.permissions === "bypass")
    return (
      <button className="ghost sm" disabled={set.isPending} onClick={() => set.mutate(null, { onSuccess: done })}>
        {t("Check permissions again")}
      </button>
    );
  return (
    <Popover
      align="right"
      label={t("Skip permission checks")}
      trigger={(open, toggle) => (
        <button className={`ghost sm ${open ? "on" : ""}`} aria-expanded={open} onClick={toggle}>
          <Icon name="shield" size={13} />
          {t("Skip permission checks")}
        </button>
      )}
    >
      {(close) => (
        <div className="stack" style={{ gap: 10, padding: 8, width: 380, maxWidth: "calc(100vw - 48px)" }}>
          <strong className="small">
            {agent.name}: {t("skip permission checks on every task")}
          </strong>
          <span className="small">
            {t("It runs with")} <code>{SKIP_FLAG[agent.adapter] ?? t("its CLI's bypass mode")}</code> {t("on PRs from the repository's team, instead of asking a reviewer. PRs from outside contributors keep the normal mode.")}
          </span>
          <span className="small muted">
            {t("In this mode the agent can run any command as the service's user, which can read the service's files: the GitHub sign-ins it keeps and the SSH keys for test machines. Turn it on only for an agent you trust with that.")}
          </span>
          <div className="row">
            <button
              className="primary sm"
              disabled={set.isPending}
              onClick={() =>
                set.mutate("bypass", {
                  onSuccess: (r) => {
                    close();
                    done(r);
                  },
                })
              }
            >
              {canChange ? t("Turn on") : t("Propose")}
            </button>
            <button className="ghost sm" onClick={close}>
              {t("Cancel")}
            </button>
          </div>
          <ErrorBox error={set.error} />
        </div>
      )}
    </Popover>
  );
}

function ChangeButton({ onClick }: { onClick: () => void }) {
  const t = useT();
  return (
    <button className="ghost sm" onClick={onClick} title={t("Ask the agent to change this")}>
      <Icon name="sparkles" size={13} />
      {t("Change")}
    </button>
  );
}

function FromSettings({ on }: { on: boolean }) {
  const t = useT();
  if (!on) return null;
  return (
    <span className="pill status-info" title={t("Set in the settings file; otherwise the default applies")}>
      {t("customized")}
    </span>
  );
}

/** Where agents run on this host: each task in a fresh container of its own, holding
 *  none of the service's settings, keys, or data, or on the host. Its network and
 *  limits apply to the next agent run; the owner of the service's repository
 *  changes them, and they are kept in this host's local.yaml. */
function SandboxCard() {
  const t = useT();
  const toast = useToast();
  const view = useQuery({
    queryKey: ["settings", "sandbox"],
    queryFn: () => api.get<SandboxView>("/api/settings/sandbox"),
    refetchInterval: (q) => (q.state.data?.enabled && !q.state.data?.ready && q.state.data?.docker ? 5000 : false),
  });
  const d = view.data;
  const [enabled, setEnabled] = useState(true);
  const [network, setNetwork] = useState<"bridge" | "host">("bridge");
  const [cpus, setCpus] = useState("2");
  const [memory, setMemory] = useState("8g");
  useEffect(() => {
    if (!d?.available) return;
    setEnabled(!!d.enabled);
    setNetwork(d.network ?? "bridge");
    setCpus(String(d.cpus ?? 2));
    setMemory(d.memory ?? "8g");
  }, [d]);
  const save = useAct(
    () => api.put<SandboxView>("/api/settings/sandbox", { enabled, network, cpus: Number(cpus), memory }),
    [["settings", "sandbox"]],
  );
  if (!d) return view.isLoading ? <Skeleton rows={3} /> : <ErrorBox error={view.error} />;
  if (!d.available) return null;
  const changed = enabled !== d.enabled || network !== d.network || Number(cpus) !== d.cpus || memory !== d.memory;
  const state = d.ready ? "executed" : d.enabled ? (d.docker ? "pending" : "failed") : "cancelled";
  const label = d.ready ? t("In containers") : d.enabled ? (d.docker ? t("Getting ready") : t("No docker")) : t("Off");
  return (
    <Section title={t("Sandbox")} hint={t("Where agents run on this host")}>
      <div className="stack" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 8 }}>
          <Status value={state} label={label} />
          {!d.ready && <span className="small muted">{d.status}</span>}
        </div>
        <span className="small muted">
          {t("Each agent task runs in a fresh container that holds only its checkout, the skills, and the agent's own login: none of the service's settings, keys, or data. Inside it the agent may do anything, and it reads GitHub as the service through a proxy that only reads.")}
        </span>
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={!d.can_configure} />
          <strong>{t("Run each agent task in a container of its own")}</strong>
        </label>
        <div className="sandbox-grid">
          <label className="stack" style={{ gap: 4 }}>
            <span className="small">{t("Network")}</span>
            <select value={network} onChange={(e) => setNetwork(e.target.value as "bridge" | "host")} disabled={!d.can_configure}>
              <option value="bridge">{t("bridge: Docker's own network")}</option>
              <option value="host">{t("host: the host's network (never its files)")}</option>
            </select>
            <span className="small muted">{t("Choose host where the host's firewall or proxy lets only the host's own traffic out.")}</span>
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="small">{t("CPUs per task")}</span>
            <input type="number" min={0.5} step={0.5} value={cpus} onChange={(e) => setCpus(e.target.value)} disabled={!d.can_configure} />
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="small">{t("Memory per task")}</span>
            <input value={memory} onChange={(e) => setMemory(e.target.value)} placeholder="8g" disabled={!d.can_configure} />
          </label>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <span className="small muted">
            {t("Image")}: <code>{d.image}</code>
          </span>
          <span className="spacer" />
          {!d.can_configure && <span className="small muted">{d.reason}</span>}
          <button
            className="primary"
            disabled={!changed || !d.can_configure || save.isPending}
            onClick={() =>
              save.mutate(undefined, {
                onSuccess: () => toast({ title: t("Saved"), body: t("The next agent task runs this way."), icon: "check" }),
              })
            }
          >
            {save.isPending ? t("Saving…") : t("Save")}
          </button>
        </div>
        <ErrorBox error={save.error} />
      </div>
    </Section>
  );
}

/** Install an agent CLI on this host (the owner of the service's repository), and
 * follow the install until it ends. */
function InstallButton({ name, source, state, canInstall }: { name: string; source: string; state?: AgentInstall; canInstall: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const install = useAct(() => api.post<AgentInstall>(`/api/settings/agents/${name}/install`), [["settings"]]);
  const running = state?.state === "running";
  useEffect(() => {
    if (!running) {
      void client.invalidateQueries({ queryKey: ["agents"] });
      return;
    }
    const timer = window.setInterval(() => void client.invalidateQueries({ queryKey: ["settings"] }), 3000);
    return () => window.clearInterval(timer);
  }, [running, client]);
  return (
    <span className="stack" style={{ gap: 2, alignItems: "flex-end", maxWidth: 320 }}>
      <button
        className="sm"
        disabled={!canInstall || running || install.isPending}
        title={canInstall ? `${t("Install from")} ${source}` : t("The owner of this service installs agents")}
        onClick={() => install.mutate(undefined)}
      >
        <Icon name="download" size={14} />
        {running ? t("Installing…") : t("Install")}
      </button>
      {running && state.lines.length > 0 && <span className="small muted truncate" style={{ maxWidth: 320 }}>{state.lines[state.lines.length - 1]}</span>}
      {state?.state === "failed" && <span className="small danger-text" style={{ whiteSpace: "normal" }}>{state.error}</span>}
      {install.error && <ErrorBox error={install.error} />}
    </span>
  );
}

function AgentsTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const health = useAgentsData();
  const byName = new Map((health.data?.health ?? []).map((h) => [h.name, h]));
  const status = (a: SettingsAgent) => {
    if (!a.enabled) return <Status value="cancelled" label={t("Off")} />;
    const h = byName.get(a.name);
    if (!h) return health.isLoading ? <span className="small muted">…</span> : <Status value="pending" label={t("Unknown")} />;
    if (!h.available) return <Status value="failed" label={t("Not installed")} />;
    if (h.authenticated === false && !a.provider) return <Status value="pending" label={t("Not signed in")} />;
    return <Status value="executed" label={h.version ? `${t("Ready")} · ${h.version}` : t("Ready")} />;
  };
  return (
    <Section title={t("Agents")} hint={t("The coding agent CLIs the service runs on this host")} aside={<ChangeButton onClick={() => ask(t("Change the agents: "))} />}>
      <DefaultAgent v={v} ask={ask} />
      <div className="list">
        {v.agents.map((a) => {
          const provider = v.providers.find((p) => p.id === a.provider);
          const choices = byName.get(a.name)?.choices;
          const modelName = (id: string) => choices?.models.find((m) => m.id === id)?.name || id;
          const budget = [a.budget.daily_usd != null ? `$${a.budget.daily_usd}/${t("day")}` : null, a.budget.daily_runs != null ? `${a.budget.daily_runs} ${t("runs/day")}` : null]
            .filter(Boolean)
            .join(" · ");
          return (
            <Row
              key={a.name}
              icon="cpu"
              title={
                <span className="row" style={{ gap: 8 }}>
                  <span className="mono" style={{ fontSize: 13.5 }}>{a.name}</span>
                  {status(a)}
                  <FromSettings on={a.from_settings} />
                  {a.permissions === "bypass" ? (
                    <span className="pill status-warn" title={t("Every task skips the CLI's permission checks on PRs from the repository's team")}>
                      {t("skips permission checks")}
                    </span>
                  ) : a.permissions === "restricted" ? (
                    <span className="pill">{t("restricted")}</span>
                  ) : a.allow_bypass ? (
                    <span className="pill status-warn">{t("bypass allowed")}</span>
                  ) : null}
                  {a.confined && (
                    <span className="pill" title={t("Runs under a command prefix, not as the service's own user")}>
                      {t("confined")}
                    </span>
                  )}
                </span>
              }
              sub={
                <>
                  {t("Model")}:{" "}
                  <strong>
                    {a.model
                      ? modelName(a.model)
                      : (provider?.models[0]?.id ?? (choices?.default_model ? `${modelName(choices.default_model)} (${t("the CLI's default")})` : t("the CLI's default")))}
                  </strong>
                  {(a.effort || choices?.default_effort) && (
                    <>
                      {" "}
                      · {t("Thinking")}: <strong>{a.effort ?? `${choices?.default_effort} (${t("default")})`}</strong>
                    </>
                  )}
                  {!a.model && provider?.models[0] ? <span className="muted"> ({t("the provider's first")})</span> : null} · {t("Through")}:{" "}
                  <strong>{provider ? provider.name || provider.id : t("its own sign-in")}</strong>
                  {budget ? ` · ${t("Budget")}: ${budget}` : ""}
                  {a.providers_supported.length === 0 && <> · {t("cannot use custom providers")}</>}
                  {a.approve_commands.length > 0 && (
                    <div className="small" style={{ marginTop: 4 }}>
                      {t("Runs without asking")}:{" "}
                      {a.approve_commands.map((c) => (
                        <code key={c} style={{ marginRight: 6, overflowWrap: "anywhere" }}>
                          {c}
                        </code>
                      ))}
                    </div>
                  )}
                </>
              }
              actions={
                <span className="row" style={{ gap: 4 }}>
                  {a.enabled && a.installed !== false && (
                    <ModelControls agent={a} choices={choices} providers={v.providers} canChange={v.access.allowed} />
                  )}
                  {a.can_bypass && <AgentModeSwitch agent={a} canChange={v.access.allowed} />}
                  {a.enabled && a.installed !== false && <ServiceSignIn name={a.name} options={a.sign_in_options} canChange={v.access.allowed} />}
                  {v.installable[a.name] && (a.installed === false || v.installs[a.name]?.state === "running") && (
                    <InstallButton name={a.name} source={v.installable[a.name]} state={v.installs[a.name]} canInstall={v.access.allowed} />
                  )}
                  <ChangeButton onClick={() => ask(`${t("For the")} ${a.name} ${t("agent")}, `)} />
                </span>
              }
            />
          );
        })}
      </div>
      {v.agents.some((a) => a.enabled && !a.confined) && (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {t("Agents without a command prefix run as this host's user, so they can read its files, including the tokens reviewers signed in with. On a shared host, confine them.")}{" "}
          <button className="link-button" style={{ color: "var(--accent)" }} onClick={() => ask(t("Confine the agents so they cannot read the service's files: "))}>
            {t("Ask how")}
          </button>
        </p>
      )}
      <p className="small muted" style={{ marginBottom: 0 }}>
        {t("Each agent runs in its CLI's auto mode; what it would ask a person goes to")} <Link to="/questions">{t("Agent questions")}</Link>. {t("Runs and costs are on")} <Link to="/agents">{t("Agents")}</Link>.
      </p>
    </Section>
  );
}

function ProvidersTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const saved = (approved: boolean) => {
    setAdding(false);
    toast({ title: approved ? t("Saved") : t("Change proposed"), icon: approved ? "check" : "inbox" });
  };
  return (
    <Section
      title={t("Service providers")}
      hint={t("Model APIs the service's agents can use instead of their CLI's own sign-in")}
      aside={
        <span className="row" style={{ gap: 6 }}>
          <ChangeButton onClick={() => ask(t("Add a model provider. Name: , API (OpenAI, Anthropic, or Gemini): , base URL: , models: , and use it for: "))} />
          {!adding && (
            <button className="primary sm" onClick={() => setAdding(true)}>
              <Icon name="plus" size={14} />
              {t("Add provider")}
            </button>
          )}
        </span>
      }
    >
      {adding && <ApiForm base="/api/settings/providers" hasKey={false} service onDone={(ok) => (ok ? saved(true) : saved(false))} />}
      {v.providers.length === 0 && !adding ? (
        <Empty icon="globe">{t("No providers. Every agent uses its CLI's own sign-in.")}</Empty>
      ) : (
        <div className="list">
          {v.providers.map((p) => (
            <ProviderRow key={p.id} p={p} canEdit={v.access.allowed} />
          ))}
        </div>
      )}
      <p className="small muted" style={{ marginBottom: 0 }}>
        {t("Keys stay on the service host. They are never shown again, never written to the memory repository, and never sent through a conversation.")}
      </p>
    </Section>
  );
}

function ProviderRow({ p, canEdit }: { p: SettingsProvider; canEdit: boolean }) {
  const t = useT();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const test = useAct(() => api.post<{ ok: boolean; models: { id: string }[]; error?: string }>(`/api/settings/providers/${p.id}/test`), []);
  const remove = useAct(() => api.del<{ approved: boolean }>(`/api/settings/providers/${p.id}`), [["settings"], ["memory"]]);
  const key = p.key;
  if (editing) {
    return (
      <div className="list-row" style={{ display: "block", padding: "12px 0" }}>
        <ApiForm
          base="/api/settings/providers"
          initial={p}
          hasKey={!!key?.configured}
          service
          onDone={(approved) => {
            setEditing(false);
            toast({ title: approved ? t("Saved") : t("Change proposed"), icon: approved ? "check" : "inbox" });
          }}
        />
      </div>
    );
  }
  return (
    <div className="list-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 8, padding: "14px 4px" }}>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <Icon name="globe" />
        <strong>{p.name || p.id}</strong>
        <span className="mono muted">{p.id}</span>
        <span className="pill status-muted">{PROTOCOLS[p.protocol] ?? p.protocol}</span>
        <span className="spacer" />
        {canEdit && (
          <button className="sm" disabled={test.isPending} onClick={() => test.mutate(undefined)} title={t("List the endpoint's models with the stored key; spends no tokens")}>
            <Icon name="activity" size={13} />
            {test.isPending ? t("Testing…") : t("Test connection")}
          </button>
        )}
        <button className="sm" onClick={() => setEditing(true)} title={canEdit ? undefined : t("Your change waits for the owner")}>
          {t("Edit")}
        </button>
        {confirm ? (
          <button className="danger sm" disabled={remove.isPending} onClick={() => remove.mutate(undefined, { onSuccess: (r) => toast({ title: r.approved ? t("Removed") : t("Change proposed"), icon: "check" }) })}>
            {t("Remove it")}
          </button>
        ) : (
          <button className="ghost sm" onClick={() => setConfirm(true)} disabled={p.used_by.length > 0} title={p.used_by.length ? t("Agents use it; point them elsewhere first") : undefined}>
            {t("Remove")}
          </button>
        )}
      </div>
      <ApiSummary
        row={p}
        extra={
          <>
            <dt>{t("Used by")}</dt>
            <dd>{p.used_by.length ? p.used_by.join(", ") : <span className="muted">{t("no agent yet")}</span>}</dd>
            <dt>{t("API key")}</dt>
            <dd>
              {!p.api_key ? (
                <span className="muted">{t("none needed")}</span>
              ) : (
                <span className="row" style={{ gap: 8 }}>
                  <code>{p.api_key}</code>
                  {key?.configured ? <Status value="executed" label={key.source === "host" ? t("set on the host") : t("set")} /> : <Status value="pending" label={t("missing")} />}
                  {key?.updated_by && (
                    <span className="small muted">
                      {key.updated_by} · <When at={key.updated_at} />
                    </span>
                  )}
                </span>
              )}
            </dd>
          </>
        }
      />
      {test.data && (
        <div className={test.data.ok ? "notice small" : "error"} style={{ marginLeft: 26 }}>
          {test.data.ok ? (
            <>
              <Icon name="check" size={13} /> {t("Connected")}: {test.data.models.length} {t("models")}
            </>
          ) : (
            test.data.error
          )}
        </div>
      )}
      <div style={{ paddingLeft: 26 }}>
        <ErrorBox error={test.error || remove.error} />
      </div>
    </div>
  );
}

/** The agent every task tries first, where it was chosen, and a way to change it. */
function DefaultAgent({ v }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const toast = useToast();
  const { name, source } = v.default_agent;
  const set = useAct((agent: string | null) => api.put<{ approved: boolean }>("/api/settings/default-agent", { agent }), [["settings"], ["memory"]]);
  // Only agents that can run here are offered.
  const ready = v.agents.filter((a) => a.enabled && a.installed !== false);
  const runsOn = v.routing.find((r) => r.task === "review")?.runs_on;
  return (
    <div className="strip default-agent">
      <Icon name="cpu" />
      <span>
        {t("Default agent")}:{" "}
        <select
          value={source === "settings" ? (name ?? "") : ""}
          disabled={!v.access.allowed || set.isPending}
          title={v.access.allowed ? undefined : t("Only the owner of this service changes agents.")}
          aria-label={t("Default agent")}
          onChange={(e) =>
            set.mutate(e.target.value || null, {
              onSuccess: (r) => toast({ title: r.approved ? t("Saved") : t("Change proposed"), icon: r.approved ? "check" : "inbox" }),
            })
          }
        >
          <option value="">
            {source === "setup" && name
              ? `${agentName(name)} (${t("chosen during setup")})`
              : `${runsOn ? agentName(runsOn) : t("none")} (${t("the first installed agent")})`}
          </option>
          {ready.map((a) => (
            <option key={a.name} value={a.name}>
              {agentName(a.name)}
            </option>
          ))}
        </select>
      </span>
      <span className="small muted">{t("Every task tries it first; others installed here take over when it cannot run.")}</span>
    </div>
  );
}

function RoutingTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  return (
    <Section
      title={t("Task routing")}
      hint={t("Which agent does each job; a repository's own settings can override these")}
      aside={<ChangeButton onClick={() => ask(t("Change which agent does which task: "))} />}
      flush
    >
      <div style={{ padding: "0 14px 10px" }}>
        <DefaultAgent v={v} ask={ask} />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t("Task")}</th>
              <th>{t("Agent")}</th>
              <th>{t("Then")}</th>
              <th className="hide-narrow">{t("Model")}</th>
              <th className="num hide-narrow">{t("Timeout")}</th>
              <th className="hide-narrow">{t("Permissions")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {v.routing.map((r) => (
              <tr key={r.task}>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="mono">{r.task}</span>
                    <FromSettings on={r.from_settings} />
                  </div>
                  <div className="small muted">{t(TASKS[r.task] ?? "")}</div>
                </td>
                <td>
                  <strong>{agentName(r.runs_on ?? r.primary)}</strong>
                  {r.runs_on && r.runs_on !== r.primary && (
                    <div className="small muted">
                      {agentName(r.primary)} {t("is not installed")}
                    </div>
                  )}
                  {!r.runs_on && <div className="small" style={{ color: "var(--bad)" }}>{t("No agent in this order is installed")}</div>}
                  {r.verifier && <div className="small muted">{t("verified by")} {r.verifier}</div>}
                </td>
                <td className="small">{r.fallback.length ? r.fallback.join(" → ") : <span className="muted">—</span>}</td>
                <td className="small hide-narrow">{r.model ?? <span className="muted">{t("agent's")}</span>}</td>
                <td className="num small hide-narrow">{Math.round(r.timeout_seconds / 60)} min</td>
                <td className="small hide-narrow">{r.permissions}</td>
                <td>
                  <ChangeButton onClick={() => ask(`${t("For the")} ${r.task} ${t("task")}, `)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function LabsTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const [adding, setAdding] = useState(false);
  return (
    <div className="stack" style={{ gap: 14 }}>
      <Section
        title={t("Test machines")}
        hint={t("where a PR's code runs to reproduce what it claims")}
        aside={
          <div className="row" style={{ gap: 6 }}>
            <ChangeButton onClick={() => ask(t("Add a test machine: "))} />
            {v.access.allowed && (
              <button className="sm" onClick={() => setAdding(!adding)}>
                <Icon name="plus" size={13} />
                {t("Add a machine")}
              </button>
            )}
          </div>
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          {t("The service reaches a machine over SSH as its own user; nothing is installed there. Write each machine's environment and rules in plain words: an agent turns them into what the service enforces, and the reproduction agent follows the rest. Each repository chooses on its own page which offered machines it uses. Each reproduction copies the PR's code into a folder of its own and removes it afterwards.")}
        </p>
        {adding && <MachineForm onDone={() => setAdding(false)} />}
        {!v.labs.length ? (
          <Empty icon="server">{t("No test machines yet. Add one, or ask the agent to.")}</Empty>
        ) : (
          <div className="list">
            {v.labs.map((m) => (
              <MachineRow key={m.name} m={m} canChange={v.access.allowed} />
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

/** A machine's connection in one line: user@host:port via its jump hosts. */
function connectionLine(m: LabMachine): string {
  const host = `${m.user ? `${m.user}@` : ""}${m.host || m.name}${m.port ? `:${m.port}` : ""}`;
  return m.proxy_jump ? `${host} via ${m.proxy_jump}` : host;
}

/** A machine's connection as an ``~/.ssh/config`` entry, for editing. */
function sshEntry(name: string, m?: Partial<LabMachine>): string {
  const lines = [`Host ${name || "gpu-01"}`];
  const fields: [string, unknown][] = [
    ["HostName", m?.host],
    ["User", m?.user],
    ["Port", m?.port],
    ["ProxyJump", m?.proxy_jump],
    ["IdentityFile", m?.identity_file],
  ];
  for (const [key, value] of fields) if (value) lines.push(`  ${key} ${value}`);
  return lines.join("\n");
}

/** What the service enforces on a machine, as a short list. */
function Enforced({ m, left }: { m: Omit<LabMachine, "name">; left?: string[] }) {
  const t = useT();
  return (
    <ul className="small" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
      <li>
        {t("Runs go in")} <code>{m.workdir}</code>, {t("at most")} {Math.round(m.timeout_seconds / 60)} {t("minutes each")}
      </li>
      <li>
        {t("Before every run")}: {m.setup ? <code style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{m.setup}</code> : <span className="muted">{t("nothing")}</span>}
      </li>
      <li>
        {t("Refused in scripts")}:{" "}
        {m.forbid.length ? (
          m.forbid.map((f) => (
            <code key={f} style={{ marginRight: 6, overflowWrap: "anywhere" }}>
              {f}
            </code>
          ))
        ) : (
          <span className="muted">{t("only the built-in patterns")}</span>
        )}
      </li>
      <li>
        {t("A GPU is free below")} {m.max_gpu_memory_mib} MiB {t("in use and at most")} {m.max_gpu_util_pct}% {t("busy")}
      </li>
      {left && left.length > 0 && (
        <li>
          {t("Left to the agent to follow")}: {left.join("; ")}
        </li>
      )}
    </ul>
  );
}

function MachineRow({ m, canChange }: { m: LabMachine; canChange: boolean }) {
  const t = useT();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const check = useAct(() => api.post<MachineCheck>(`/api/settings/labs/${m.name}/check`), []);
  const remove = useAct(() => api.del<{ approved: boolean }>(`/api/settings/labs/${m.name}`), [["settings"], ["memory"]]);
  const c = check.data;
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="list-row" style={{ padding: "12px 4px", alignItems: "flex-start" }}>
        <span style={{ paddingTop: 2 }}>
          <Icon name="server" />
        </span>
        <span className="main stack" style={{ gap: 6 }}>
          <span className="t" style={{ whiteSpace: "normal" }}>
            {m.name} <span className="muted small mono">{m.transport === "ssh" ? connectionLine(m) : t("this host")}</span>
          </span>
          <span className="small muted">
            {m.repos.length ? `${t("offered only to")} ${m.repos.join(", ")}` : t("offered to every repository")}
          </span>
          {m.description ? (
            <span className="small" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {m.description}
            </span>
          ) : (
            <span className="small muted">{t("No environment or rules written yet.")}</span>
          )}
          <Enforced m={m} />
          {c && (
            <span className={`small ${c.ok ? "" : "danger-text"}`}>
              {c.ok ? (
                <>
                  <Icon name="check" size={12} /> {c.system} · {c.cpus} CPUs · {c.memory_gb} GB
                  {c.gpus?.length ? ` · ${c.gpus.filter((g) => g.idle).length}/${c.gpus.length} ${t("GPUs idle")}` : ` · ${t("no GPUs")}`}
                </>
              ) : (
                <>
                  <Icon name="x" size={12} /> {c.error}
                </>
              )}
            </span>
          )}
          <ErrorBox error={check.error || remove.error} />
        </span>
        {canChange && (
          <span className="row" style={{ gap: 4 }}>
            <button className="sm" disabled={check.isPending} onClick={() => check.mutate(undefined)}>
              {check.isPending ? t("Checking…") : t("Check")}
            </button>
            <button className="ghost sm" onClick={() => setEditing(!editing)} aria-expanded={editing}>
              {editing ? t("Cancel") : t("Edit")}
            </button>
            <button
              className="ghost sm danger-text"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(undefined, {
                  onSuccess: (r) => toast({ title: r.approved ? t("Removed") : t("Change proposed"), icon: "check" }),
                })
              }
            >
              {t("Remove")}
            </button>
          </span>
        )}
      </div>
      {editing && <MachineForm initial={m} onDone={() => setEditing(false)} />}
    </div>
  );
}

const DEFAULT_MACHINE: Omit<LabMachine, "name"> = {
  description: "",
  transport: "ssh",
  host: null,
  workdir: "am-labs",
  setup: "",
  repos: [],
  forbid: [],
  timeout_seconds: 1800,
  max_gpu_memory_mib: 500,
  max_gpu_util_pct: 0,
};

/** A machine in three parts: its name, how to reach it (an ``~/.ssh/config`` entry, or
 * this host), and which repositories may use it; then its environment and rules in plain
 * language. An agent drafts what the service enforces from that text (run folder,
 * setup, time limit, refused commands, GPU thresholds), shown with the settings diff
 * before Save applies it. */
function MachineForm({ initial, onDone }: { initial?: LabMachine; onDone: () => void }) {
  const t = useT();
  const toast = useToast();
  const onboarded = useRepos();
  const [name, setName] = useState(initial?.name ?? "");
  const [transport, setTransport] = useState<"ssh" | "local">(initial?.transport ?? "ssh");
  const [ssh, setSsh] = useState(initial ? sshEntry(initial.name, initial) : "");
  const [repos, setRepos] = useState<string[]>(initial?.repos ?? []);
  const [description, setDescription] = useState(initial?.description ?? "");
  const [draft, setDraft] = useState<{ for: string; result: MachineDraft } | null>(null);
  const repoOptions = Array.from(new Set([...(onboarded.data ?? []).map((r) => r.repo), ...(initial?.repos ?? [])]));
  const base: Omit<LabMachine, "name"> = initial ?? DEFAULT_MACHINE;
  // The enforced settings follow the description: a changed description needs a new draft.
  const changed = description.trim() !== (initial?.description ?? "").trim();
  const current = draft && draft.for === description.trim() ? draft.result : null;
  const needsDraft = changed && description.trim() !== "" && !current;
  const body = (m: Omit<LabMachine, "name">) => ({
    machine: { ...m, name: undefined, transport, description: description.trim(), repos },
    ssh_config: transport === "ssh" ? ssh : null,
  });
  const drafting = useAct(() => api.post<MachineDraft>(`/api/settings/labs/${name.trim()}/draft`, body(base)), []);
  const save = useAct(
    () => api.put<{ approved: boolean }>(`/api/settings/labs/${name.trim()}`, body(current ? current.machine : base)),
    [["settings"], ["labs"], ["memory"]],
  );
  return (
    <form
      className="card stack"
      style={{ gap: 12, marginBottom: 12 }}
      onSubmit={(e) => {
        e.preventDefault();
        if (needsDraft) {
          drafting.mutate(undefined, { onSuccess: (r) => setDraft({ for: description.trim(), result: r }) });
          return;
        }
        save.mutate(undefined, {
          onSuccess: (r) => {
            toast({ title: r.approved ? (initial ? t("Saved") : t("Machine added")) : t("Change proposed"), icon: "server" });
            onDone();
          },
        });
      }}
    >
      <div className="grid2" style={{ gap: 10 }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Name")}</span>
          <input
            required
            disabled={!!initial}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="gpu-01"
            pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*"
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Reached")}</span>
          <select value={transport} onChange={(e) => setTransport(e.target.value as "ssh" | "local")}>
            <option value="ssh">{t("over SSH")}</option>
            <option value="local">{t("on this host")}</option>
          </select>
        </label>
      </div>
      {transport === "ssh" && (
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("How to connect, written like an entry in ~/.ssh/config: HostName, User, Port, ProxyJump, IdentityFile (a key on the service's host)")}</span>
          <textarea
            className="mono-input"
            rows={6}
            value={ssh}
            onChange={(e) => setSsh(e.target.value)}
            placeholder={"Host gpu-01\n  HostName 10.0.0.5\n  User ci\n  Port 2222\n  ProxyJump bastion.example.com\n  IdentityFile ~/.ssh/id_ed25519"}
            spellCheck={false}
          />
        </label>
      )}
      <div className="stack" style={{ gap: 6 }}>
        <span className="small muted">
          {t("Offer only to these repositories (optional)")} · {repos.length ? `${repos.length} ${t("selected")}` : t("offered to every repository")}
        </span>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }} role="group" aria-label={t("Offer only to these repositories (optional)")}>
          <button
            type="button"
            className={`chip-btn ${repos.length === 0 ? "on" : ""}`}
            style={{ borderStyle: "solid", height: 30 }}
            aria-pressed={repos.length === 0}
            onClick={() => setRepos([])}
          >
            <Icon name={repos.length === 0 ? "check" : "repo"} size={13} />
            {t("All repositories")}
          </button>
          {repoOptions.map((r) => {
            const on = repos.includes(r);
            return (
              <button
                key={r}
                type="button"
                className={`chip-btn ${on ? "on" : ""}`}
                style={{ borderStyle: "solid", height: 30 }}
                aria-pressed={on}
                onClick={() => setRepos((prev) => (prev.includes(r) ? prev.filter((x) => x !== r) : [...prev, r]))}
              >
                <Icon name={on ? "check" : "repo"} size={13} />
                {r}
              </button>
            );
          })}
        </div>
      </div>
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">{t("Environment and rules, in plain words: the hardware, how to set up the environment, where data lives, what runs must not do, how long they may take")}</span>
        <textarea
          rows={6}
          style={{ fontFamily: "inherit", lineHeight: 1.5 }}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t("8× A100 80GB, CUDA 12.4. Activate the conda environment rlinf before running. Put runs in /data/am-runs. Use only GPUs 0–3; the others belong to another team. Never run ray stop or write to /data/datasets. A run may take at most two hours.")}
        />
      </label>
      {current && (
        <section className="stack" style={{ gap: 8 }}>
          <span className="kind-chip">{t("What the service will enforce")}</span>
          <Enforced m={current.machine} left={current.left_to_agent} />
          {current.problems.length > 0 && (
            <ul className="small danger-text" style={{ margin: 0, paddingLeft: 18 }}>
              {current.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
          {current.diff && (
            <details>
              <summary className="small muted">{t("The change to the settings")}</summary>
              <pre className="diff">{current.diff}</pre>
            </details>
          )}
        </section>
      )}
      <div className="row">
        <button className="primary" disabled={!name.trim() || save.isPending || drafting.isPending || (current?.problems.length ?? 0) > 0}>
          {drafting.isPending ? t("Reading the description…") : needsDraft ? t("Draft the settings") : t("Save")}
        </button>
        <button type="button" className="ghost" onClick={onDone}>
          {t("Cancel")}
        </button>
        {needsDraft && <span className="small muted">{t("An agent turns the description into what the service enforces; you see it before saving.")}</span>}
      </div>
      <ErrorBox error={save.error || drafting.error} />
    </form>
  );
}

function SystemTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const custom = new Set(v.system.from_settings);
  const h = v.host;
  return (
    <div className="grid2">
      <Section title={t("Behavior")} aside={<ChangeButton onClick={() => ask(t("Change the system settings: "))} />}>
        <div className="list">
          <Row
            icon="clock"
            title={
              <span className="row" style={{ gap: 6 }}>
                {t("Check repositories every")} <FromSettings on={custom.has("poll_seconds")} />
              </span>
            }
            sub={t("How often the service looks for new PRs, comments, and CI results")}
            value={<strong>{v.system.poll_seconds >= 60 ? `${Math.round(v.system.poll_seconds / 60)} min` : `${v.system.poll_seconds} s`}</strong>}
          />
          <Row
            icon="shield"
            title={
              <span className="row" style={{ gap: 6 }}>
                {t("Public writes per hour")} <FromSettings on={custom.has("max_public_writes_per_hour")} />
              </span>
            }
            sub={t("Comments, labels, and reviews across all repositories; a safety cap")}
            value={<strong>{v.system.max_public_writes_per_hour}</strong>}
          />
          <Row
            icon="repo"
            title={
              <span className="row" style={{ gap: 6 }}>
                {t("Public writes per repository per hour")} <FromSettings on={custom.has("max_repo_public_writes_per_hour")} />
              </span>
            }
            value={<strong>{v.system.max_repo_public_writes_per_hour}</strong>}
          />
        </div>
      </Section>
      <Section
        title={t("This service")}
        hint={t("the host setup")}
        aside={
          v.access.allowed ? (
            <Link className="button" to="/setup" style={{ height: 30, fontSize: 12.5 }}>
              <Icon name="settings" size={13} />
              {t("Change host setup")}
            </Link>
          ) : null
        }
      >
        <dl className="props">
          <dt>{t("Owner")}</dt>
          <dd>{v.owner ? <strong>{v.owner}</strong> : <span className="muted">{t("nobody yet")}</span>}</dd>
          <dt>{t("Service repository")}</dt>
          <dd>{h.code_repo ? <code>{h.code_repo}</code> : <span className="muted">—</span>}</dd>
          <dt>{t("Memory repository")}</dt>
          <dd>{h.memory_repo ? <code>{h.memory_repo}</code> : <span className="muted">{t("local only")}</span>}</dd>
          <dt>{t("Dashboard address")}</dt>
          <dd>{h.base_url ?? <span className="muted">—</span>}</dd>
          <dt>{t("Mode")}</dt>
          <dd>
            {h.dev_mode ? (
              <Status value="pending" label={h.sandbox_repo ? `${t("dev, acts only on")} ${h.sandbox_repo}` : t("dev, everything in shadow")} />
            ) : (
              <Status value="executed" label={t("production")} />
            )}{" "}
            {h.paused && <Status value="failed" label={t("paused")} />}
          </dd>
          <dt>{t("Operators")}</dt>
          <dd>
            {h.operators.join(", ") || "—"}
            <div className="small muted">{t("They run the host. They cannot change these settings unless they own the service.")}</div>
          </dd>
        </dl>
      </Section>
    </div>
  );
}

function FileTab({ v }: { v: SettingsView }) {
  const t = useT();
  const toast = useToast();
  const [text, setText] = useState(v.document);
  const [title, setTitle] = useState("settings: update");
  const [checked, setChecked] = useState<SettingsCheck | null>(null);
  const [base, setBase] = useState(v.document);
  useEffect(() => {
    // follow the stored document while there are no local edits
    if (text === base) setText(v.document);
    setBase(v.document);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [v.document]);
  const check = useAct((yaml: string) => api.post<SettingsCheck>("/api/settings/check", { yaml }), []);
  const save = useAct(
    (yaml: string) => api.put<{ approved: boolean; change: { id: string; state: string } | null }>("/api/settings", { yaml, title }),
    [["settings"], ["audit"]],
  );
  const dirty = text !== v.document;
  return (
    <Section
      title={<code style={{ background: "transparent", padding: 0, fontSize: 14 }}>config/service.yaml</code>}
      hint={t("in the memory repository; only what differs from the defaults")}
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        {t("Asking the agent is easier; this is the same document it edits. Keys are named here (api_key: DEEPSEEK_API_KEY) and set on the Model providers tab.")}
      </p>
      <textarea
        value={text}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          setChecked(null);
        }}
        rows={Math.min(28, Math.max(10, text.split("\n").length + 2))}
        placeholder={"schema: 1\nproviders:\n  my-gateway:\n    protocol: openai\n    base_url: https://gateway.example/v1\n    api_key: MY_GATEWAY_KEY\n    models: [some-model]\nagents:\n  codex: {provider: my-gateway}\n"}
      />
      <div className="row" style={{ marginTop: 8 }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} aria-label={t("Change title")} style={{ flex: 1, minWidth: 200 }} />
        <button disabled={!dirty || check.isPending} onClick={() => check.mutate(text, { onSuccess: setChecked })}>
          {t("Check")}
        </button>
        <button
          className="primary"
          disabled={!dirty || save.isPending || (checked !== null && !checked.ok)}
          onClick={() =>
            save.mutate(text, {
              onSuccess: (r) => {
                setChecked(null);
                toast({
                  title: r.approved ? t("Settings applied") : t("Change proposed"),
                  body: r.approved ? undefined : t("The owner of this service applies it."),
                  icon: r.approved ? "check" : "inbox",
                });
              },
            })
          }
        >
          {v.access.allowed ? t("Save and apply") : t("Propose")}
        </button>
        {dirty && (
          <button className="ghost" onClick={() => (setText(v.document), setChecked(null))}>
            {t("Revert")}
          </button>
        )}
      </div>
      {checked && (
        <div className="stack" style={{ marginTop: 10 }}>
          {checked.ok ? (
            <div className="notice small">
              <Icon name="check" size={13} /> {t("It checks. This is what changes:")}
            </div>
          ) : (
            <div className="error">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {checked.problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          <Diff text={checked.diff} />
        </div>
      )}
      <ErrorBox error={save.error || check.error} />
    </Section>
  );
}
