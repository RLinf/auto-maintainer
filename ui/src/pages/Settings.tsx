// Settings: how the service runs. Agents, model providers, default routes, and system
// values in one place, each row showing its current value. Changes are made by asking
// the agent (it drafts the edit, you apply it) or by editing the settings file; API keys
// go only into their key fields. Only maintainers of the service's repository apply
// changes; anyone else's wait for one.

import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type SettingsAgent, type SettingsCheck, type SettingsProvider, type SettingsView } from "../api";
import { useAgent, type AgentContext } from "../agent";
import { Diff, Empty, ErrorBox, PageHead, Section, Status, useAct, When } from "../components";
import { useSettings } from "../data";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { useAgentsData } from "./Agents";
import { SettingsChangeCard } from "../settingsChange";
import { Skeleton, Tabs, useTab, useToast } from "../ui";

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
  hygiene: "Checks PR conventions",
  onboard: "Drafts a new repository's profile",
  learn: "Turns feedback into lessons",
  chat: "Answers in conversations",
  self_improve: "Changes the service's own code",
  probe: "Health checks",
};

const PROTOCOL: Record<string, string> = {
  anthropic: "Anthropic Messages",
  openai: "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
};

export default function SettingsPage() {
  const t = useT();
  const agent = useAgent();
  const view = useSettings();
  const tabs = [
    { id: "agents", label: t("Agents"), count: view.data?.agents.filter((a) => a.enabled).length },
    { id: "providers", label: t("Model providers"), count: view.data?.providers.length },
    { id: "routing", label: t("Task routing") },
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
      {tab === "agents" && <AgentsTab v={v} ask={ask} />}
      {tab === "providers" && <ProvidersTab v={v} ask={ask} />}
      {tab === "routing" && <RoutingTab v={v} ask={ask} />}
      {tab === "system" && <SystemTab v={v} ask={ask} />}
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
        <textarea
          value={text}
          rows={Math.min(5, Math.max(1, text.split("\n").length))}
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
      <div className="list">
        {v.agents.map((a) => {
          const provider = v.providers.find((p) => p.id === a.provider);
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
                  {a.allow_bypass && <span className="pill status-warn">{t("bypass allowed")}</span>}
                </span>
              }
              sub={
                <>
                  {t("Model")}:{" "}
                  <strong>{a.model ?? provider?.models[0] ?? t("the CLI's default")}</strong>
                  {!a.model && provider?.models[0] ? <span className="muted"> ({t("the provider's first")})</span> : null} · {t("Through")}:{" "}
                  <strong>{provider ? provider.name || provider.id : t("its own sign-in")}</strong>
                  {budget ? ` · ${t("Budget")}: ${budget}` : ""}
                  {a.providers_supported.length === 0 && <> · {t("cannot use custom providers")}</>}
                </>
              }
              actions={<ChangeButton onClick={() => ask(`${t("For the")} ${a.name} ${t("agent")}, `)} />}
            />
          );
        })}
      </div>
      <p className="small muted" style={{ marginBottom: 0 }}>
        {t("Each agent runs in its CLI's auto mode; what it would ask a person goes to")} <Link to="/questions">{t("Agent questions")}</Link>. {t("Runs and costs are on")} <Link to="/agents">{t("Agents")}</Link>.
      </p>
    </Section>
  );
}

function ProvidersTab({ v, ask }: { v: SettingsView; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  return (
    <Section
      title={t("Model providers")}
      hint={t("Endpoints agents use instead of their CLI's own sign-in")}
      aside={
        <button className="primary sm" onClick={() => ask(t("Add a model provider. Name: , protocol (OpenAI-compatible or Anthropic): , base URL: , models: , and use it for: "))}>
          <Icon name="plus" size={14} />
          {t("Add provider")}
        </button>
      }
    >
      {v.providers.length === 0 ? (
        <Empty icon="globe">
          {t("No custom providers. Every agent uses its CLI's own sign-in.")}
          <div className="small" style={{ marginTop: 6 }}>
            {t("Ask the agent to add one, then paste its key here.")}
          </div>
        </Empty>
      ) : (
        <div className="list">
          {v.providers.map((p) => (
            <ProviderRow key={p.id} p={p} canEdit={v.access.allowed} ask={ask} />
          ))}
        </div>
      )}
      <p className="small muted" style={{ marginBottom: 0 }}>
        {t("Keys stay on the service host. They are never shown again, never written to the memory repository, and never sent through a conversation.")}
      </p>
    </Section>
  );
}

function ProviderRow({ p, canEdit, ask }: { p: SettingsProvider; canEdit: boolean; ask: (p: string, send?: boolean) => void }) {
  const t = useT();
  const toast = useToast();
  const [editing, setEditing] = useState(!!p.api_key && !p.key?.configured && canEdit);
  const [value, setValue] = useState("");
  const [show, setShow] = useState(false);
  const save = useAct((v: string) => api.put(`/api/settings/secrets/${p.api_key}`, { value: v }), [["settings"]]);
  const test = useAct(() => api.post<{ ok: boolean; models: string[]; error?: string }>(`/api/settings/providers/${p.id}/test`), []);
  const key = p.key;
  return (
    <div className="list-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 8, padding: "14px 4px" }}>
      <div className="row">
        <Icon name="globe" />
        <strong>{p.name || p.id}</strong>
        <span className="mono muted">{p.id}</span>
        <span className="pill status-muted">{PROTOCOL[p.protocol] ?? p.protocol}</span>
        <span className="spacer" />
        {canEdit && (
          <button
            className="sm"
            disabled={test.isPending}
            onClick={() => test.mutate(undefined)}
            title={t("List the endpoint's models with the stored key; spends no tokens")}
          >
            <Icon name="activity" size={13} />
            {test.isPending ? t("Testing…") : t("Test connection")}
          </button>
        )}
        <ChangeButton onClick={() => ask(`${t("For the provider")} ${p.id}, `)} />
      </div>
      <dl className="props" style={{ paddingLeft: 26 }}>
        <dt>{t("Base URL")}</dt>
        <dd>
          <code>{p.base_url}</code>
        </dd>
        <dt>{t("Models")}</dt>
        <dd className="labels">{p.models.length ? p.models.map((m) => <span key={m} className="label">{m}</span>) : <span className="muted">—</span>}</dd>
        <dt>{t("Used by")}</dt>
        <dd>{p.used_by.length ? p.used_by.join(", ") : <span className="muted">{t("no agent yet")}</span>}</dd>
        <dt>{t("API key")}</dt>
        <dd>
          {!p.api_key ? (
            <span className="muted">{t("none needed")}</span>
          ) : (
            <span className="row" style={{ gap: 8 }}>
              <code>{p.api_key}</code>
              {key?.configured ? (
                <Status value="executed" label={key.source === "host" ? t("set on the host") : t("set")} />
              ) : (
                <Status value="pending" label={t("missing")} />
              )}
              {key?.updated_by && (
                <span className="small muted">
                  {key.updated_by} · <When at={key.updated_at} />
                </span>
              )}
              {canEdit && !editing && (
                <button className="ghost sm" onClick={() => setEditing(true)}>
                  <Icon name="key" size={13} />
                  {key?.configured ? t("Replace") : t("Set key")}
                </button>
              )}
            </span>
          )}
        </dd>
      </dl>
      {editing && p.api_key && (
        <form
          className="row"
          style={{ paddingLeft: 26 }}
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(value.trim(), {
              onSuccess: () => {
                setValue("");
                setEditing(false);
                toast({ title: `${p.api_key} ${t("saved")}`, body: t("The agents that use it restart with the new key."), icon: "key" });
              },
            });
          }}
        >
          <input
            type={show ? "text" : "password"}
            autoComplete="off"
            spellCheck={false}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={key?.configured ? t("•••••••• (leave empty to keep the stored key)") : t("Paste the API key")}
            aria-label={`${p.api_key} ${t("value")}`}
            style={{ flex: 1, minWidth: 220, fontFamily: "var(--mono)" }}
          />
          <button type="button" className="ghost sm" onClick={() => setShow(!show)} aria-label={show ? t("Hide") : t("Show")}>
            <Icon name="eye" size={14} />
          </button>
          <button className="primary sm" disabled={!value.trim() || save.isPending}>
            {t("Save key")}
          </button>
          <button type="button" className="ghost sm" onClick={() => (setEditing(false), setValue(""))}>
            {t("Cancel")}
          </button>
        </form>
      )}
      {test.data && (
        <div className={test.data.ok ? "notice small" : "error"} style={{ marginLeft: 26 }}>
          {test.data.ok ? (
            <>
              <Icon name="check" size={13} /> {t("Connected")}: {test.data.models.length} {t("models")}
              {test.data.models.length > 0 && <span className="muted"> ({test.data.models.slice(0, 8).join(", ")}{test.data.models.length > 8 ? ", …" : ""})</span>}
            </>
          ) : (
            test.data.error
          )}
        </div>
      )}
      <div style={{ paddingLeft: 26 }}>
        <ErrorBox error={save.error || test.error} />
      </div>
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
                  <strong>{r.primary}</strong>
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
      <Section title={t("This service")} hint={t("set on the host")}>
        <dl className="props">
          <dt>{t("Settings owned by")}</dt>
          <dd>{v.repo ? <code>{v.repo}</code> : <span className="muted">{t("no repository")}</span>}</dd>
          <dt>{t("Code repository")}</dt>
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
            <div className="small muted">{t("They run the host. They cannot change these settings unless they are maintainers.")}</div>
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
                  body: r.approved ? undefined : t("A maintainer of the service's repository applies it."),
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
