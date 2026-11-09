// Agent accounts: signing an agent CLI in from the dashboard (for the service, by its
// maintainers; for oneself, by anyone), and each person's own agents: their own
// sign-in or model API, used only for the work they ask for.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type AgentLogin, type MyAgent, type PersonalProvider } from "./api";
import { copyText } from "./clipboard";
import { Empty, ErrorBox, Section, Status, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";
import { AgentsUsageSection } from "./usage";

const PROTOCOLS: Record<string, string> = {
  anthropic: "Anthropic Messages",
  openai: "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
};

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

/** Whether an agent CLI is signed in for the service, and a sign-in for maintainers
 * of the service's repository. */
export function ServiceSignIn({ name, canChange }: { name: string; canChange: boolean }) {
  const t = useT();
  const status = useQuery({ queryKey: ["service-sign-ins"], queryFn: () => api.get<Record<string, boolean | null>>("/api/settings/agents/sign-ins"), staleTime: 30_000 });
  const [login, setLogin] = useState<AgentLogin | null>(null);
  const start = useAct(() => api.post<AgentLogin>(`/api/settings/agents/${name}/sign-in`), []);
  const signed = status.data?.[name];
  if (login) return <SignInFlow login={login} onEnd={() => setLogin(null)} />;
  if (signed === undefined || signed === null) return null;
  return (
    <span className="row" style={{ gap: 6 }}>
      {signed ? <Status value="executed" label={t("Signed in")} /> : <Status value="pending" label={t("Not signed in")} />}
      {canChange && (
        <button className="sm" disabled={start.isPending} onClick={() => start.mutate(undefined, { onSuccess: (l) => setLogin(l) })} title={t("Sign this agent in for the service")}>
          <Icon name="key" size={13} />
          {signed ? t("Sign in again") : t("Sign in")}
        </button>
      )}
      <ErrorBox error={start.error} />
    </span>
  );
}

/** The agents installed here, as the signed-in person runs them for the work they ask
 * for: on the service's accounts, or on their own sign-in or model API. */
export function MyAgentsTab() {
  const t = useT();
  const q = useQuery({ queryKey: ["my-agents"], queryFn: () => api.get<{ personal: boolean; agents: MyAgent[] }>("/api/me/agents") });
  return (
    <div className="stack" style={{ gap: 16 }}>
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
              <MyAgentRow key={a.name} agent={a} />
            ))}
          </div>
        )}
      </Section>
      <AgentsUsageSection mine />
    </div>
  );
}

function MyAgentRow({ agent: a }: { agent: MyAgent }) {
  const t = useT();
  const [login, setLogin] = useState<AgentLogin | null>(a.login);
  const [editing, setEditing] = useState(false);
  const keys = [["my-agents"]];
  const save = useAct((body: { use_own: boolean; provider: PersonalProvider | null }) => api.put<MyAgent>(`/api/me/agents/${a.name}`, body), keys);
  const signIn = useAct(() => api.post<AgentLogin>(`/api/me/agents/${a.name}/sign-in`), []);
  const signOut = useAct(() => api.post<MyAgent>(`/api/me/agents/${a.name}/sign-out`), keys);
  const own = a.setup.use_own;
  const via = a.setup.provider ? t("your own model API") : t("your own sign-in");
  const ready = a.setup.provider ? a.setup.has_key : a.signed_in === true;
  return (
    <div className="list-row" style={{ alignItems: "flex-start", flexDirection: "column", gap: 8 }}>
      <span className="row" style={{ gap: 8, flexWrap: "wrap", width: "100%" }}>
        <span className="mono" style={{ fontSize: 13.5 }}>
          {a.name}
        </span>
        {a.can_sign_in && (a.signed_in ? <Status value="executed" label={t("You are signed in")} /> : <Status value="pending" label={t("You are not signed in")} />)}
        {own ? <span className="pill status-ok">{`${t("Your requests run on")} ${via}`}</span> : <span className="pill">{t("Your requests run on the service's account")}</span>}
        <span className="right row" style={{ gap: 4, marginLeft: "auto" }}>
          {a.can_sign_in && !login && (
            <button className="sm" disabled={signIn.isPending} onClick={() => signIn.mutate(undefined, { onSuccess: (l) => setLogin(l) })}>
              <Icon name="key" size={13} />
              {a.signed_in ? t("Sign in again") : t("Sign in")}
            </button>
          )}
          {a.can_sign_in && a.signed_in && (
            <button className="ghost sm" disabled={signOut.isPending} onClick={() => signOut.mutate(undefined)}>
              <Icon name="logout" size={13} />
              {t("Sign out")}
            </button>
          )}
          {a.protocols.length > 0 && (
            <button className="ghost sm" onClick={() => setEditing(!editing)}>
              {t("Own model API")}
            </button>
          )}
          <label className="row small" style={{ gap: 4 }} title={a.isolated ? "" : t("Agents run on the host here, where every run could read every person's sign-in, so personal accounts are off")}>
            <input
              type="checkbox"
              checked={own}
              disabled={!a.isolated || save.isPending || (!own && !ready)}
              onChange={(e) => save.mutate({ use_own: e.target.checked, provider: a.setup.provider })}
            />
            {t("Use for my requests")}
          </label>
        </span>
      </span>
      {!a.isolated && <span className="small muted">{t("Agents run on the host here, where every run could read every person's sign-in, so personal accounts are off.")}</span>}
      {!own && !ready && a.isolated && (
        <span className="small muted">{a.can_sign_in ? t("Sign in, or set your own model API, to use your own account.") : t("Set your own model API to use your own account.")}</span>
      )}
      {login && <SignInFlow login={login} onEnd={() => setLogin(null)} />}
      {editing && <ProviderForm agent={a} onDone={() => setEditing(false)} />}
      <ErrorBox error={save.error || signIn.error || signOut.error} />
    </div>
  );
}

/** The person's own model API for one agent: protocol, address, models, and a
 * write-only key. */
function ProviderForm({ agent: a, onDone }: { agent: MyAgent; onDone: () => void }) {
  const t = useT();
  const p = a.setup.provider;
  const [protocol, setProtocol] = useState(p?.protocol ?? a.protocols[0] ?? "");
  const [baseUrl, setBaseUrl] = useState(p?.base_url ?? "");
  const [models, setModels] = useState((p?.models ?? []).join(", "));
  const [key, setKey] = useState("");
  const keys = [["my-agents"]];
  const save = useAct(async () => {
    await api.put(`/api/me/agents/${a.name}`, {
      use_own: a.setup.use_own,
      provider: { protocol, base_url: baseUrl.trim(), models: models.split(/[\s,]+/).filter(Boolean) },
    });
    if (key.trim()) await api.put(`/api/me/agents/${a.name}/key`, { value: key.trim() });
  }, keys);
  const remove = useAct(async () => {
    await api.put(`/api/me/agents/${a.name}`, { use_own: false, provider: null });
    await api.del(`/api/me/agents/${a.name}/key`);
  }, keys);
  return (
    <form
      className="card stack"
      style={{ gap: 8, padding: 12, background: "var(--surface-2)", width: "100%" }}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(undefined, { onSuccess: () => { setKey(""); onDone(); } });
      }}
    >
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">{t("API protocol")}</span>
        <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
          {a.protocols.map((x) => (
            <option key={x} value={x}>
              {PROTOCOLS[x] ?? x}
            </option>
          ))}
        </select>
      </label>
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">{t("Base URL")}</span>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" spellCheck={false} />
      </label>
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">{t("Models, the first by default")}</span>
        <input value={models} onChange={(e) => setModels(e.target.value)} placeholder="model-a, model-b" spellCheck={false} />
      </label>
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">{a.setup.has_key ? t("API key (set; leave empty to keep it)") : t("API key")}</span>
        <input type="password" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" spellCheck={false} />
      </label>
      <span className="row" style={{ gap: 6 }}>
        <button className="primary sm" disabled={!protocol || !baseUrl.trim() || save.isPending}>
          {t("Save")}
        </button>
        {p && (
          <button type="button" className="ghost sm" disabled={remove.isPending} onClick={() => remove.mutate(undefined, { onSuccess: onDone })}>
            {t("Remove my model API")}
          </button>
        )}
        <button type="button" className="ghost sm" onClick={onDone}>
          {t("Cancel")}
        </button>
      </span>
      <ErrorBox error={save.error || remove.error} />
    </form>
  );
}
