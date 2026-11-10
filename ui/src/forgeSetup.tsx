// The forges this service serves, and adding one (design 5.16): maintainers of the
// service's repository pick its kind and address, then sign the service in there as
// the account it will act as, with the forge CLI's one-time code or a GitLab access
// token. The service restarts with the new forge in place and keeps it only if it
// starts.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import { copyText } from "./clipboard";
import { ErrorBox, Section, Status } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

interface ForgeRow {
  id: string;
  kind: string;
  web_url: string;
  default: boolean;
  signed_in_as: string | null;
}

interface Started {
  flow_id?: string;
  code?: string;
  url?: string;
  done?: boolean;
}

const KINDS: Record<string, string> = { github: "GitHub", gitlab: "GitLab" };

/** ``text`` with each ``{name}`` replaced by ``parts[name]``. */
function fill(text: string, parts: Record<string, ReactNode>): ReactNode {
  return text.split(/(\{\w+\})/).map((piece, i) => {
    const name = piece.match(/^\{(\w+)\}$/)?.[1];
    return <Fragment key={i}>{name && name in parts ? parts[name] : piece}</Fragment>;
  });
}
const PUBLIC: Record<string, string> = { github: "https://github.com", gitlab: "https://gitlab.com" };

export function ForgesCard({ canChange }: { canChange: boolean }) {
  const t = useT();
  const q = useQuery({ queryKey: ["forges-setup"], queryFn: () => api.get<{ forges: ForgeRow[] }>("/api/settings/forges"), retry: false });
  const [adding, setAdding] = useState(false);
  if (q.error instanceof ApiError && q.error.status === 404) return null;
  return (
    <Section
      title={t("Forges")}
      hint={t("Where the repositories this service maintains live; it acts as one account on each.")}
      aside={
        canChange && !adding ? (
          <button className="sm" onClick={() => setAdding(true)}>
            <Icon name="plus" size={14} />
            {t("Add a forge")}
          </button>
        ) : null
      }
    >
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : q.error || !q.data ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="list">
          {q.data.forges.map((f) => (
            <div key={f.id} className="list-row">
              <span className="main">
                <span className="t row" style={{ gap: 6 }}>
                  <span className="mono">{f.id}</span>
                  <span className="pill">{KINDS[f.kind] ?? f.kind}</span>
                  {f.default && <span className="pill status-info">{t("default")}</span>}
                </span>
                <span className="meta">
                  {f.web_url}
                  {f.default ? ` · ${t("its names carry no prefix")}` : ` · ${t("names on it begin with")} ${f.id}:`}
                </span>
              </span>
              {f.signed_in_as ? <Status value="executed" label={`${t("acts as")} ${f.signed_in_as}`} /> : <Status value="pending" label={t("not signed in")} />}
            </div>
          ))}
        </div>
      )}
      {adding && <AddForge onClose={() => setAdding(false)} />}
    </Section>
  );
}

function AddForge({ onClose }: { onClose: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [kind, setKind] = useState("gitlab");
  const [id, setId] = useState("");
  const [web, setWeb] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [apiUrl, setApiUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [how, setHow] = useState<"code" | "token">("code");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [started, setStarted] = useState<Started | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const selfManaged = kind === "gitlab" && web.trim() !== "" && web.trim().replace(/\/+$/, "") !== PUBLIC.gitlab;
  const codeNeedsApp = selfManaged && !clientId.trim();
  const useToken = kind === "gitlab" && (how === "token" || codeNeedsApp);

  const finished = (forges: { added: string; signed_in_as: string }[] | undefined) => {
    setDone(forges?.[0]?.signed_in_as ?? id);
    void client.invalidateQueries();
  };

  useEffect(() => {
    if (!started?.flow_id || done) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.post<{ pending?: boolean; forges?: { added: string; signed_in_as: string }[] }>("/api/settings/forges/poll", { flow_id: started.flow_id });
        if (stop) return;
        if (r.pending) {
          window.setTimeout(tick, 2500);
          return;
        }
        finished(r.forges);
      } catch (e) {
        if (!stop) {
          setError(e);
          setStarted(null);
        }
      }
    };
    window.setTimeout(tick, 2500);
    return () => {
      stop = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started?.flow_id, done]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<Started & { forges?: { added: string; signed_in_as: string }[] }>("/api/settings/forges", {
        id: id.trim(),
        kind,
        web_url: web.trim(),
        api_url: apiUrl.trim(),
        oauth_client_id: clientId.trim(),
        token: useToken ? token.trim() : "",
      });
      if (r.done) finished(r.forges);
      else setStarted(r);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="card stack" style={{ gap: 8, padding: 12, background: "var(--surface-2)" }}>
        <span className="row" style={{ gap: 6 }}>
          <Status value="executed" label={t("Forge added")} />
          <span className="small">
            {fill(t("The service now serves {forge} as {login}. Add its repositories as {example}."), {
              forge: <span className="mono">{id}</span>,
              login: <strong>{done}</strong>,
              example: <span className="mono">{id}:group/project</span>,
            })}
          </span>
        </span>
        <span>
          <button className="ghost sm" onClick={onClose}>
            {t("Close")}
          </button>
        </span>
      </div>
    );
  }

  if (started?.code) {
    return (
      <div className="card stack" style={{ gap: 8, padding: 12, background: "var(--surface-2)" }}>
        <span className="small">
          1.{" "}
          <a href={started.url} target="_blank" rel="noreferrer noopener">
            {t("Open the sign-in page")} <Icon name="external" size={12} />
          </a>{" "}
          {t("and sign in as the account the service will act as.")}
        </span>
        <span className="small row" style={{ gap: 8 }}>
          2. {t("Enter this code there:")}
          <code style={{ fontSize: 16, letterSpacing: 2 }}>{started.code}</code>
          <button className="ghost sm" onClick={() => void copyText(started.code ?? "")} title={t("Copy")}>
            <Icon name="check" size={13} />
          </button>
        </span>
        <span className="small muted">{t("Waiting for the forge; the service restarts with it once you have signed in.")}</span>
        <ErrorBox error={error} />
        <span>
          <button className="ghost sm" onClick={onClose}>
            {t("Cancel")}
          </button>
        </span>
      </div>
    );
  }

  return (
    <form
      className="card stack"
      style={{ gap: 10, padding: 12, background: "var(--surface-2)" }}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Kind")}</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="gitlab">GitLab</option>
            <option value="github">GitHub</option>
          </select>
        </label>
        <label className="stack" style={{ gap: 4, flex: "1 1 140px" }}>
          <span className="small muted">{t("Id: names on it begin with it")}</span>
          <input value={id} onChange={(e) => setId(e.target.value.toLowerCase())} placeholder="corp" spellCheck={false} />
        </label>
        <label className="stack" style={{ gap: 4, flex: "3 1 240px" }}>
          <span className="small muted">{t("Address (empty for the public one)")}</span>
          <input value={web} onChange={(e) => setWeb(e.target.value)} placeholder={PUBLIC[kind]} spellCheck={false} />
        </label>
      </div>
      <button type="button" className="link-button small" style={{ alignSelf: "flex-start", color: "var(--info)" }} onClick={() => setAdvanced(!advanced)}>
        <Icon name={advanced ? "chevronDown" : "chevronRight"} size={13} />
        {advanced ? t("Fewer options") : t("More options")}
      </button>
      {advanced && (
        <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          <label className="stack" style={{ gap: 4, flex: "1 1 240px" }}>
            <span className="small muted">{t("API address (when not the usual one)")}</span>
            <input value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} placeholder={kind === "gitlab" ? "https://gitlab.example.com/api/v4" : "https://ghe.example.com/api/v3"} spellCheck={false} />
          </label>
          {kind === "gitlab" && (
            <label className="stack" style={{ gap: 4, flex: "1 1 240px" }}>
              <span className="small muted">{t("OAuth application client id (self-managed GitLab, for the one-time code)")}</span>
              <input value={clientId} onChange={(e) => setClientId(e.target.value)} spellCheck={false} />
            </label>
          )}
        </div>
      )}
      <div className="stack" style={{ gap: 6 }}>
        <span className="small muted">{t("Sign the service in there, as the account it will act as:")}</span>
        <label className="row small" style={{ gap: 6 }}>
          <input type="radio" checked={!useToken} onChange={() => setHow("code")} disabled={codeNeedsApp} />
          {t("With a one-time code on the forge's site")}
          {codeNeedsApp && <span className="muted">({t("needs the OAuth application's client id")})</span>}
        </label>
        {kind === "gitlab" && (
          <label className="row small" style={{ gap: 6, flexWrap: "wrap" }}>
            <input type="radio" checked={useToken} onChange={() => setHow("token")} />
            {t("With a personal access token (scopes api, read_user)")}
            {useToken && <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" style={{ flex: 1, minWidth: 200 }} />}
          </label>
        )}
      </div>
      <ErrorBox error={error} />
      <span className="row" style={{ gap: 6 }}>
        <button className="primary sm" disabled={busy || !id.trim() || (useToken && !token.trim())}>
          {busy ? t("Signing in…") : t("Add and sign in")}
        </button>
        <button type="button" className="ghost sm" onClick={onClose}>
          {t("Cancel")}
        </button>
      </span>
    </form>
  );
}
