// Agent accounts: signing an agent CLI in from the dashboard (for the service, by its
// owner; for oneself, by anyone), and each person's own agents: their own sign-in or
// one of their own model APIs, used only for the work they ask for.

import { AgentOptionsForm, setCount } from "./agentOptions";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type AgentChoices, type AgentLogin, type MyAgent, type MyProvider, type SignInOption } from "./api";
import { copyText } from "./clipboard";
import { Empty, ErrorBox, Section, Status, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useAgentsData } from "./pages/Agents";
import { Skeleton, useToast } from "./ui";
import { agentName } from "./prActions";
import { ApiForm, ApiSummary, KeyState, modelsFor, PROTOCOLS } from "./providerForm";
import { AgentsUsageSection } from "./usage";

/** Inline text input for a model name when the CLI does not list its models:
 * saves on Enter or blur when changed, and Escape reverts. */
export function ModelInput({
  value,
  placeholder,
  ariaLabel,
  title,
  disabled,
  onSave,
}: {
  value: string | null | undefined;
  placeholder: string;
  ariaLabel: string;
  title?: string;
  disabled: boolean;
  onSave: (next: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  const skipBlur = useRef(false);
  useEffect(() => setDraft(value ?? ""), [value]);
  const commit = (raw: string) => {
    const next = raw.trim() || null;
    if (next !== (value || null)) onSave(next);
  };
  return (
    <input
      className="model-input"
      value={draft}
      disabled={disabled}
      placeholder={placeholder}
      aria-label={ariaLabel}
      title={title}
      autoComplete="off"
      spellCheck={false}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={(e) => {
        if (skipBlur.current) {
          skipBlur.current = false;
          return;
        }
        commit(e.currentTarget.value);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          skipBlur.current = true;
          commit(e.currentTarget.value);
          e.currentTarget.blur();
        } else if (e.key === "Escape") {
          skipBlur.current = true;
          setDraft(value ?? "");
          e.currentTarget.blur();
        }
      }}
    />
  );
}

/** A sign-in in progress: the page to open, the code to enter there or a box for the
 * code the page shows, until the CLI says it is signed in. */
export function SignInFlow({ login, onEnd }: { login: AgentLogin; onEnd: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [code, setCode] = useState("");
  const q = useQuery({
    queryKey: ["agent-login", login.id],
    queryFn: () => api.get<AgentLogin>(`/api/agent-logins/${login.id}`),
    initialData: login,
    refetchInterval: (query) => (["starting", "waiting"].includes(query.state.data?.state ?? "") ? 1500 : false),
  });
  const s = q.data ?? login;
  const send = useAct(() => api.post<AgentLogin>(`/api/agent-logins/${login.id}/code`, { code }), [["agent-login", login.id]]);
  const cancel = useAct(() => api.del<AgentLogin>(`/api/agent-logins/${login.id}`), [["agent-login", login.id]]);
  const ended = !["starting", "waiting"].includes(s.state);
  useEffect(() => {
    if (ended) {
      void client.invalidateQueries({ queryKey: ["my-agents"] });
      void client.invalidateQueries({ queryKey: ["service-sign-ins"] });
      void client.invalidateQueries({ queryKey: ["settings"] });
    }
  }, [ended, client]);
  return (
    <div className="card stack" style={{ gap: 8, padding: 12, background: "var(--surface-2)" }}>
      {s.state === "done" ? (
        <span className="row" style={{ gap: 6 }}>
          <Status value="executed" label={t("Signed in")} />
          <button className="ghost sm" onClick={onEnd}>
            {t("Close")}
          </button>
        </span>
      ) : s.state === "failed" || s.state === "cancelled" ? (
        <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <Status value="failed" label={s.state === "failed" ? t("Sign-in failed") : t("Cancelled")} />
          {s.message && <span className="small muted">{s.message}</span>}
          <button className="ghost sm" onClick={onEnd}>
            {t("Close")}
          </button>
        </span>
      ) : !s.url ? (
        <span className="small muted">{t("Starting the sign-in…")}</span>
      ) : (
        <>
          <span className="small">
            1.{" "}
            <a href={s.url} target="_blank" rel="noreferrer noopener">
              {t("Open the sign-in page")} <Icon name="external" size={12} />
            </a>{" "}
            {t("and sign in to the account to use.")}
          </span>
          {s.code && (
            <span className="small row" style={{ gap: 8 }}>
              2. {t("Enter this code there:")}
              <code style={{ fontSize: 16, letterSpacing: 2 }}>{s.code}</code>
              <button className="ghost sm" onClick={() => void copyText(s.code)} title={t("Copy")}>
                <Icon name="check" size={13} />
              </button>
            </span>
          )}
          {s.needs_code && (
            <form
              className="row form-row"
              style={{ gap: 6 }}
              onSubmit={(e) => {
                e.preventDefault();
                if (code.trim()) send.mutate(undefined, { onSuccess: () => setCode("") });
              }}
            >
              <span className="small">2. {t("Paste the code the page shows:")}</span>
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" spellCheck={false} className="grow-input" />
              <button className="primary sm" disabled={!code.trim() || send.isPending}>
                {t("Send")}
              </button>
            </form>
          )}
          {!s.code && !s.needs_code && s.message && <span className="small muted">{s.message}</span>}
          <span>
            <button className="ghost sm" onClick={() => cancel.mutate(undefined)}>
              {t("Cancel")}
            </button>
          </span>
        </>
      )}
      <ErrorBox error={send.error || cancel.error} />
    </div>
  );
}

/** A button that starts a CLI's sign-in, with a choice of what to sign in to when
 * the CLI offers several (pi: a Claude, ChatGPT, or Copilot subscription). */
function StartSignIn({ url, options, signed, title, onStarted }: { url: string; options: SignInOption[]; signed: boolean | null | undefined; title?: string; onStarted: (l: AgentLogin) => void }) {
  const t = useT();
  const [option, setOption] = useState(options[0]?.id ?? "");
  const start = useAct(() => api.post<AgentLogin>(url, options.length ? { option } : {}), []);
  return (
    <span className="row" style={{ gap: 4 }}>
      {options.length > 0 && (
        <select value={option} onChange={(e) => setOption(e.target.value)} aria-label={t("Sign in to")} title={t("Sign in to")}>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      )}
      <button className="sm" disabled={start.isPending} onClick={() => start.mutate(undefined, { onSuccess: onStarted })} title={title}>
        <Icon name="key" size={13} />
        {signed ? t("Sign in again") : t("Sign in")}
      </button>
      <ErrorBox error={start.error} />
    </span>
  );
}

/** Whether an agent CLI is signed in for the service, and a sign-in for its owner. A
 * CLI with no sign-in (dsh) only says whether it has a model API key. */
export function ServiceSignIn({ name, options = [], canSignIn, canChange }: { name: string; options?: SignInOption[]; canSignIn: boolean; canChange: boolean }) {
  const t = useT();
  const status = useQuery({ queryKey: ["service-sign-ins"], queryFn: () => api.get<Record<string, boolean | null>>("/api/settings/agents/sign-ins"), staleTime: 30_000 });
  const [login, setLogin] = useState<AgentLogin | null>(null);
  const signed = status.data?.[name];
  if (login) return <SignInFlow login={login} onEnd={() => setLogin(null)} />;
  if (signed === undefined || signed === null) return null;
  if (!canSignIn)
    return signed ? (
      <Status value="executed" label={t("Has a model API key")} />
    ) : (
      <span title={t("This CLI has no sign-in: it runs only on a model API. Choose one for it under Model providers.")}>
        <Status value="pending" label={t("Needs a model API")} />
      </span>
    );
  return (
    <span className="row" style={{ gap: 6 }}>
      {signed ? <Status value="executed" label={t("Signed in")} /> : <Status value="pending" label={t("Not signed in")} />}
      {canChange && <StartSignIn url={`/api/settings/agents/${name}/sign-in`} options={options} signed={signed} title={t("Sign this agent in for the service")} onStarted={setLogin} />}
    </span>
  );
}

interface MyAgents {
  personal: boolean;
  agents: MyAgent[];
  providers: MyProvider[];
  default: { agent: string; own: boolean } | null;
  service_default: string | null;
}

function useMine() {
  return useQuery({ queryKey: ["my-agents"], queryFn: () => api.get<MyAgents>("/api/me/agents") });
}

/** The agents installed here, as the signed-in person runs them for the work they ask
 * for: on the service's accounts, or on their own sign-in or one of their own model
 * APIs, with the model and reasoning level they choose. */
export function MyAgentsSection() {
  const t = useT();
  const q = useMine();
  const health = useAgentsData();
  const byName = new Map((health.data?.health ?? []).map((h) => [h.name, h]));
  return (
    <>
      <Section title={t("My agents")} hint={t("Run what you ask for on your own agent accounts instead of the service's. Nobody else's work uses them, and your keys never leave this host.")}>
        {q.isLoading ? (
          <Skeleton rows={3} />
        ) : q.error || !q.data ? (
          <ErrorBox error={q.error} />
        ) : q.data.agents.length === 0 ? (
          <Empty icon="cpu">{t("No agents on this host yet.")}</Empty>
        ) : (
          <div className="list">
            {q.data.agents.map((a) => (
              <MyAgentRow key={a.name} agent={a} apis={q.data.providers} choices={byName.get(a.name)?.choices ?? a.choices} personal={q.data.personal} />
            ))}
          </div>
        )}
      </Section>
      <AgentsUsageSection mine />
    </>
  );
}

/** The agent what the person asks for runs on when they pick none: any agent here
 * on the service's account (those not signed in for the service are shown but
 * cannot be chosen), or one they run on their own account; by default, their own
 * agents first and then the service's default. */
export function MyDefaultChoice() {
  const t = useT();
  const q = useMine();
  const set = useAct((body: { agent: string | null; own: boolean }) => api.put("/api/me/agents-default", body), [["my-agents"], ["settings"]]);
  if (!q.data) return q.error ? <ErrorBox error={q.error} /> : <Skeleton rows={1} />;
  const data = q.data;
  const mine = data.agents.filter((a) => a.setup.use_own);
  const value = data.default ? `${data.default.own ? "own" : "service"}:${data.default.agent}` : "";
  const service = agentName(data.service_default) || t("none");
  return (
    <div className="agent-choice">
      <label className="agent-choice-label" htmlFor="my-default-agent">
        {t("What I ask for runs on")}
      </label>
      <select
        id="my-default-agent"
        value={value}
        disabled={set.isPending}
        onChange={(e) => {
          const [account, ...name] = e.target.value.split(":");
          set.mutate(e.target.value ? { agent: name.join(":"), own: account === "own" } : { agent: null, own: false });
        }}
      >
        <option value="">{mine.length ? `${t("my own agents, then")} ${service}` : `${service} (${t("the service's default")})`}</option>
        <optgroup label={t("On the service's account")}>
          {data.agents.map((a) => (
            <option key={`service:${a.name}`} value={`service:${a.name}`} disabled={a.service_usable === false && value !== `service:${a.name}`}>
              {a.service_usable === false ? `${agentName(a.name)} (${t("not signed in for the service")})` : agentName(a.name)}
            </option>
          ))}
        </optgroup>
        {mine.length > 0 && (
          <optgroup label={t("On my own account")}>
            {mine.map((a) => (
              <option key={`own:${a.name}`} value={`own:${a.name}`}>
                {agentName(a.name)}
              </option>
            ))}
          </optgroup>
        )}
      </select>
      <span className="small muted">{t("What you ask for runs on it when you pick no agent; when it cannot run, the service's other agents take over. Your own accounts run only what you ask for.")}</span>
      <ErrorBox error={set.error} />
    </div>
  );
}

/** The person's own model APIs: only their own agents use them, for what they ask. */
export function MyApisSection() {
  const t = useT();
  const q = useMine();
  const [adding, setAdding] = useState(false);
  if (q.data && !q.data.personal) return null;
  return (
    <Section
      title={t("My model APIs")}
      hint={t("Yours alone: only your own agents use them, for the work you ask for, and your keys stay on this host.")}
      aside={
        !adding && (
          <button className="primary sm" onClick={() => setAdding(true)}>
            <Icon name="plus" size={14} />
            {t("Add model API")}
          </button>
        )
      }
    >
      {adding && <ApiForm base="/api/me/providers" hasKey={false} service={false} onDone={() => setAdding(false)} />}
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : q.error || !q.data ? (
        <ErrorBox error={q.error} />
      ) : q.data.providers.length === 0 && !adding ? (
        <Empty icon="globe">{t("No model APIs of your own. Add one, then choose it for an agent under My agents.")}</Empty>
      ) : (
        <div className="list">
          {q.data.providers.map((p) => (
            <MyApiRow key={p.id} p={p} />
          ))}
        </div>
      )}
    </Section>
  );
}

function MyApiRow({ p }: { p: MyProvider }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const remove = useAct(() => api.del(`/api/me/providers/${p.id}`), [["my-agents"], ["settings"]]);
  if (editing) {
    return (
      <div className="list-row" style={{ display: "block", padding: "12px 0" }}>
        <ApiForm base="/api/me/providers" initial={p} hasKey={p.has_key} service={false} onDone={() => setEditing(false)} />
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
        <KeyState set={p.has_key} />
        <span className="spacer" />
        <button className="sm" onClick={() => setEditing(true)}>
          {t("Edit")}
        </button>
        {confirm ? (
          <button className="danger sm" disabled={remove.isPending} onClick={() => remove.mutate(undefined)}>
            {t("Remove it")}
          </button>
        ) : (
          <button className="ghost sm" onClick={() => setConfirm(true)} title={p.used_by.length ? t("Your agents on it go back to their own sign-in") : undefined}>
            {t("Remove")}
          </button>
        )}
      </div>
      <ApiSummary
        row={p}
        extra={
          <>
            <dt>{t("Used by")}</dt>
            <dd>{p.used_by.length ? p.used_by.join(", ") : <span className="muted">{t("none of your agents yet")}</span>}</dd>
          </>
        }
      />
      <ErrorBox error={remove.error} />
    </div>
  );
}

type Setup = MyAgent["setup"];

function MyAgentRow({
  agent: a,
  apis,
  choices,
  personal,
}: {
  agent: MyAgent;
  apis: MyProvider[];
  choices: AgentChoices | null | undefined;
  personal: boolean;
}) {
  const t = useT();
  const [login, setLogin] = useState<AgentLogin | null>(a.login);
  const keys = [["my-agents"], ["settings"]];
  const save = useAct((body: Omit<Setup, "agent">) => api.put<MyAgent>(`/api/me/agents/${a.name}`, body), keys);
  const signOut = useAct(() => api.post<MyAgent>(`/api/me/agents/${a.name}/sign-out`), keys);
  const s = a.setup;
  const usable = apis.filter((p) => modelsFor(p, a.adapter).length > 0 || p.id === s.provider);
  const current = s.use_own ? apis.find((p) => p.id === s.provider) : undefined;
  const providerModels = current ? modelsFor(current, a.adapter) : [];
  const serviceProviderModels = !s.use_own ? a.service_provider_models ?? null : null;
  const cliModels = choices?.models ?? [];
  const nameOf = (id: string | null | undefined) => (id ? cliModels.find((m) => m.id === id)?.name || id : null);
  const cliModel = nameOf(choices?.default_model);
  const canPickModel = a.adapter !== "generic";
  const spec = current
    ? current.models.find((m) => m.id === (s.model ?? providerModels[0]?.id))
    : serviceProviderModels
      ? serviceProviderModels.find((m) => m.id === (s.model ?? a.service_model ?? serviceProviderModels[0]?.id))
      : undefined;
  const efforts =
    current || serviceProviderModels
      ? spec?.reasoning.length
        ? spec.reasoning
        : spec?.tested_at
          ? []
          : (choices?.efforts ?? [])
      : (choices?.efforts ?? []);
  const choice = !s.use_own ? "" : s.provider ? `api:${s.provider}` : "sign-in";
  const disabled = !personal || save.isPending;
  const defaultEffortLabel = current
    ? `${t("Thinking")}: ${t("default")}`
    : !s.use_own && a.service_effort
      ? `${t("Thinking")}: ${a.service_effort} (${t("service default")})`
      : choices?.default_effort
        ? `${t("Thinking")}: ${choices.default_effort} (${t("default")})`
        : `${t("Thinking")}: ${!s.use_own ? t("service default") : t("default")}`;
  const defaultCliModelLabel = !s.use_own
    ? a.service_model
      ? `${nameOf(a.service_model)} (${t("service default")})`
      : cliModel
        ? `${cliModel} (${t("the CLI's default")})`
        : t("service default")
    : cliModel
      ? `${cliModel} (${t("the CLI's default")})`
      : t("the CLI's default");
  const choose = (value: string) =>
    save.mutate(
      value === ""
        ? { use_own: false, provider: null, model: null, effort: null }
        : value === "sign-in"
          ? { use_own: true, provider: null, model: null, effort: null }
          : { use_own: true, provider: value.slice(4), model: null, effort: null },
    );
  return (
    <div className="list-row" style={{ alignItems: "flex-start", flexDirection: "column", gap: 8 }}>
      <span className="row" style={{ gap: 8, flexWrap: "wrap", width: "100%" }}>
        <span className="mono" style={{ fontSize: 13.5 }}>
          {a.name}
        </span>
        {a.can_sign_in && a.signed_in && <Status value="executed" label={t("You are signed in")} />}
        {a.isolated && !a.can_sign_in && usable.length === 0 && (
          <span
            className="small muted"
            title={
              a.protocols.length > 0
                ? `${t("This CLI has no sign-in: it runs only on a model API. To run it on your own, add one under My model APIs that speaks")} ${a.protocols.map((p) => PROTOCOLS[p as keyof typeof PROTOCOLS] ?? p).join(` ${t("or")} `)}.`
                : t("This CLI has no sign-in the dashboard can run and takes no custom model API, so it runs on the service's account.")
            }
          >
            {a.protocols.length > 0 ? t("needs a model API of yours") : t("the service's account only")}
          </span>
        )}
        <span className="right row model-controls" style={{ gap: 4 }}>
          <select
            value={choice}
            disabled={!a.isolated || disabled}
            aria-label={`${a.name}: ${t("Your requests run on")}`}
            title={a.isolated ? t("What runs the work you ask for") : t("Agents run on the host here, where every run could read every person's sign-in, so personal accounts are off")}
            onChange={(e) => choose(e.target.value)}
          >
            <option value="">{t("the service's account")}</option>
            {(a.can_sign_in || choice === "sign-in") && (
              <option value="sign-in" disabled={!a.signed_in && choice !== "sign-in"}>
                {a.signed_in ? t("my own sign-in") : `${t("my own sign-in")} (${t("sign in first")})`}
              </option>
            )}
            {usable.map((p) => (
              <option key={p.id} value={`api:${p.id}`}>
                {`${t("my API")}: ${p.name || p.id}`}
              </option>
            ))}
          </select>
          {current ? (
            <select
              value={s.model ?? ""}
              disabled={disabled}
              aria-label={`${a.name}: ${t("Model")}`}
              onChange={(e) => save.mutate({ use_own: true, provider: s.provider, model: e.target.value || null, effort: null })}
            >
              <option value="">{providerModels[0] ? `${providerModels[0].id} (${t("the first")})` : t("the first")}</option>
              {providerModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
              {s.model && !providerModels.some((m) => m.id === s.model) && <option value={s.model}>{s.model}</option>}
            </select>
          ) : serviceProviderModels ? (
            <select
              value={s.model ?? ""}
              disabled={disabled}
              aria-label={`${a.name}: ${t("Model")}`}
              onChange={(e) => save.mutate({ use_own: false, provider: null, model: e.target.value || null, effort: null })}
            >
              <option value="">{a.service_model ? `${a.service_model} (${t("service default")})` : t("service default")}</option>
              {serviceProviderModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
              {s.model && !serviceProviderModels.some((m) => m.id === s.model) && <option value={s.model}>{s.model}</option>}
            </select>
          ) : cliModels.length > 0 ? (
            <select
              value={s.model ?? ""}
              disabled={disabled}
              aria-label={`${a.name}: ${t("Model")}`}
              onChange={(e) => save.mutate({ use_own: s.use_own, provider: null, model: e.target.value || null, effort: s.effort })}
            >
              <option value="">{defaultCliModelLabel}</option>
              {cliModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name || m.id}
                </option>
              ))}
              {s.model && !cliModels.some((m) => m.id === s.model) && <option value={s.model}>{s.model}</option>}
            </select>
          ) : canPickModel ? (
            <ModelInput
              value={s.model}
              disabled={disabled}
              ariaLabel={`${a.name}: ${t("Model")}`}
              placeholder={`${t("Model")}: ${defaultCliModelLabel}`}
              title={t("Model name for this CLI; leave empty for the default. Press Enter to save.")}
              onSave={(next) => save.mutate({ use_own: s.use_own, provider: null, model: next, effort: s.effort })}
            />
          ) : null}
          {(efforts.length > 0 || s.effort) && (
            <select
              value={s.effort ?? ""}
              disabled={disabled}
              aria-label={`${a.name}: ${t("Thinking")}`}
              onChange={(e) => save.mutate({ use_own: s.use_own, provider: s.use_own ? s.provider : null, model: s.model, effort: e.target.value || null })}
            >
              <option value="">{defaultEffortLabel}</option>
              {efforts.map((e) => (
                <option key={e} value={e}>
                  {t("Thinking")}: {e}
                </option>
              ))}
              {s.effort && !efforts.includes(s.effort) && (
                <option value={s.effort}>
                  {t("Thinking")}: {s.effort}
                </option>
              )}
            </select>
          )}
          {a.can_sign_in && !login && <StartSignIn url={`/api/me/agents/${a.name}/sign-in`} options={a.sign_in_options} signed={a.signed_in} onStarted={setLogin} />}
          {a.can_sign_in && a.signed_in && (
            <button className="ghost sm" disabled={signOut.isPending} onClick={() => signOut.mutate(undefined)}>
              <Icon name="logout" size={13} />
              {t("Sign out")}
            </button>
          )}
        </span>
      </span>
      {!a.isolated && <span className="small muted">{t("Agents run on the host here, where every run could read every person's sign-in, so personal accounts are off.")}</span>}
      {login && <SignInFlow login={login} onEnd={() => setLogin(null)} />}
      {a.isolated && a.option_schema.length > 0 && <MyAgentOptions agent={a} />}
      <ErrorBox error={save.error || signOut.error} />
    </div>
  );
}

/** The CLI settings of this person's own account of an agent, over the service's:
 * they reach only the runs of the work this person asks for on their own account. */
function MyAgentOptions({ agent: a }: { agent: MyAgent }) {
  const t = useT();
  const toast = useToast();
  const save = useAct((options: Record<string, unknown>) => api.put<MyAgent>(`/api/me/agents/${a.name}/options`, { options }), [["my-agents"]]);
  const n = setCount(a.setup.options, a.option_schema);
  return (
    <details className="agent-options-box" style={{ width: "100%" }}>
      <summary className="small">
        {t("CLI settings for my account")} · {n ? `${n} ${t("set")}` : t("as the service's")}
      </summary>
      <p className="small muted" style={{ margin: "6px 0 10px" }}>
        {a.setup.use_own
          ? t("Your runs on your own account use them; one you leave unset takes the service's. Nobody else's runs see them.")
          : t("They apply once your requests run on your own account; one you leave unset takes the service's.")}
      </p>
      <AgentOptionsForm
        schema={a.option_schema}
        value={a.setup.options ?? {}}
        inherited={a.service_options}
        canChange
        saving={save.isPending}
        error={save.error}
        onSave={(options) => save.mutate(options, { onSuccess: () => toast({ title: t("Saved"), icon: "check" }) })}
      />
    </details>
  );
}
