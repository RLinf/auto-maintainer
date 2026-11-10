// Agent accounts: signing an agent CLI in from the dashboard (for the service, by its
// owner; for oneself, by anyone), and each person's own agents: their own sign-in or
// one of their own model APIs, used only for the work they ask for.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type AgentLogin, type MyAgent, type MyProvider, type SignInOption } from "./api";
import { copyText } from "./clipboard";
import { Empty, ErrorBox, Section, Status, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";
import { ApiForm, ApiSummary, KeyState, modelsFor, PROTOCOLS } from "./providerForm";
import { AgentsUsageSection } from "./usage";

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
              className="row"
              style={{ gap: 6 }}
              onSubmit={(e) => {
                e.preventDefault();
                if (code.trim()) send.mutate(undefined, { onSuccess: () => setCode("") });
              }}
            >
              <span className="small">2. {t("Paste the code the page shows:")}</span>
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" spellCheck={false} style={{ flex: 1, minWidth: 160 }} />
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

/** Whether an agent CLI is signed in for the service, and a sign-in for its owner. */
export function ServiceSignIn({ name, options = [], canChange }: { name: string; options?: SignInOption[]; canChange: boolean }) {
  const t = useT();
  const status = useQuery({ queryKey: ["service-sign-ins"], queryFn: () => api.get<Record<string, boolean | null>>("/api/settings/agents/sign-ins"), staleTime: 30_000 });
  const [login, setLogin] = useState<AgentLogin | null>(null);
  const signed = status.data?.[name];
  if (login) return <SignInFlow login={login} onEnd={() => setLogin(null)} />;
  if (signed === undefined || signed === null) return null;
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
              <MyAgentRow key={a.name} agent={a} apis={q.data.providers} />
            ))}
          </div>
        )}
      </Section>
      <AgentsUsageSection mine />
    </>
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
  const remove = useAct(() => api.del(`/api/me/providers/${p.id}`), [["my-agents"]]);
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

function MyAgentRow({ agent: a, apis }: { agent: MyAgent; apis: MyProvider[] }) {
  const t = useT();
  const [login, setLogin] = useState<AgentLogin | null>(a.login);
  const keys = [["my-agents"]];
  const save = useAct((body: Omit<Setup, "agent">) => api.put<MyAgent>(`/api/me/agents/${a.name}`, body), keys);
  const signOut = useAct(() => api.post<MyAgent>(`/api/me/agents/${a.name}/sign-out`), keys);
  const s = a.setup;
  const usable = apis.filter((p) => modelsFor(p, a.adapter).length > 0 || p.id === s.provider);
  const current = apis.find((p) => p.id === s.provider);
  const models = current ? modelsFor(current, a.adapter) : [];
  const spec = current?.models.find((m) => m.id === (s.model ?? models[0]?.id));
  const choice = !s.use_own ? "" : s.provider ? `api:${s.provider}` : "sign-in";
  const choose = (value: string) =>
    save.mutate(
      value === ""
        ? { use_own: false, provider: s.provider, model: s.model, effort: s.effort }
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
        {a.can_sign_in && (a.signed_in ? <Status value="executed" label={t("You are signed in")} /> : <Status value="pending" label={t("You are not signed in")} />)}
        <span className="right row model-controls" style={{ gap: 4, marginLeft: "auto" }}>
          <select
            value={choice}
            disabled={!a.isolated || save.isPending}
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
          {current && (
            <select value={s.model ?? ""} disabled={save.isPending} aria-label={`${a.name}: ${t("Model")}`} onChange={(e) => save.mutate({ ...s, model: e.target.value || null, effort: null })}>
              <option value="">{models[0] ? `${models[0].id} (${t("the first")})` : t("the first")}</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          )}
          {current && spec && spec.reasoning.length > 0 && (
            <select value={s.effort ?? ""} disabled={save.isPending} aria-label={`${a.name}: ${t("Thinking")}`} onChange={(e) => save.mutate({ ...s, effort: e.target.value || null })}>
              <option value="">{`${t("Thinking")}: ${t("default")}`}</option>
              {spec.reasoning.map((e) => (
                <option key={e} value={e}>
                  {t("Thinking")}: {e}
                </option>
              ))}
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
      {a.isolated && !a.can_sign_in && usable.length === 0 && (
        <span className="small muted">
          {a.protocols.length > 0
            ? t("This CLI has no sign-in the dashboard can run; add a model API of your own it can use, under Model providers.")
            : t("This CLI has no sign-in the dashboard can run and takes no custom model API, so it runs on the service's account.")}
        </span>
      )}
      {login && <SignInFlow login={login} onEnd={() => setLogin(null)} />}
      <ErrorBox error={save.error || signOut.error} />
    </div>
  );
}
