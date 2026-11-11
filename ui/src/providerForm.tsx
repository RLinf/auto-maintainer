// Model APIs on the Settings page: the service's providers (the owner's to change) and
// each person's own (theirs alone, used only by their own agents for the work they ask
// for). One form serves both. A person fills in an address and a key; "Find models"
// reads the endpoint's list, and "Test" holds real conversations with a model to learn
// which APIs it answers on and whether it calls tools, reads images, reasons, and
// streams, so nobody fills those in by hand.

import { useState } from "react";
import { api, type ApiRow, type Examination, type ModelList, type ModelSpec, type Protocol } from "./api";
import { ErrorBox, Status, useAct, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";

export const PROTOCOLS: Record<Protocol, string> = {
  "openai-responses": "OpenAI Responses",
  openai: "OpenAI Chat Completions",
  anthropic: "Anthropic Messages",
  google: "Google Gemini API",
};

const PRESETS: { label: string; protocol: Protocol; base_url: string; name: string }[] = [
  { label: "OpenAI", protocol: "openai-responses", base_url: "https://api.openai.com/v1", name: "OpenAI" },
  { label: "Anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com", name: "Anthropic" },
  { label: "Google Gemini", protocol: "google", base_url: "https://generativelanguage.googleapis.com/v1beta", name: "Gemini" },
  { label: "DeepSeek", protocol: "openai", base_url: "https://api.deepseek.com/v1", name: "DeepSeek" },
  { label: "OpenRouter", protocol: "openai", base_url: "https://openrouter.ai/api/v1", name: "OpenRouter" },
  { label: "vLLM / SGLang / Ollama", protocol: "openai", base_url: "http://HOST:8000/v1", name: "Self-hosted" },
];

const EMPTY: Omit<ModelSpec, "id"> = {
  context_window: null,
  max_output: null,
  reasoning: [],
  interfaces: [],
  tools: null,
  images: null,
  streaming: null,
  tested_at: null,
};

export function blankModel(id: string): ModelSpec {
  return { id, ...EMPTY };
}

/** The models of ``row`` that the adapter ``adapter`` can run. */
export function modelsFor(row: ApiRow, adapter: string): ModelSpec[] {
  return row.models.filter((m) => (row.works_with[m.id] ?? []).includes(adapter));
}

function tokens(n: number | null): string {
  if (!n) return "—";
  return n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

function slug(text: string): string {
  const s = text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return /^[a-z]/.test(s) ? s : s ? `api-${s}` : "";
}

/** What a model can do, as small marks: tested or not, and each ability. */
function Abilities({ m }: { m: ModelSpec }) {
  const t = useT();
  const mark = (on: boolean | null, label: string) =>
    on === null ? null : (
      <span className={`pill ${on ? "status-ok" : "status-muted"}`} title={on ? label : `${t("not")}: ${label}`}>
        {on ? "" : "✕ "}
        {label}
      </span>
    );
  return (
    <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>
      {mark(m.tools, t("tools"))}
      {mark(m.images, t("images"))}
      {mark(m.streaming, t("streaming"))}
      {m.reasoning.length > 0 && <span className="pill status-info">{`${t("reasons")}: ${m.reasoning.join(", ")}`}</span>}
      {m.interfaces.map((p) => (
        <span key={p} className="label" title={t("Answered a conversation on this API")}>
          {PROTOCOLS[p] ?? p}
        </span>
      ))}
      {m.tested_at ? (
        <span className="small muted">
          {t("tested")} <When at={m.tested_at} />
        </span>
      ) : (
        <span className="small muted">{t("not tested")}</span>
      )}
    </span>
  );
}

/** A saved model API, read only, with what each model works with. */
export function ApiSummary({ row, extra }: { row: ApiRow; extra?: React.ReactNode }) {
  const t = useT();
  return (
    <dl className="props provider-indent">
      <dt>{t("Base URL")}</dt>
      <dd>
        <code>{row.base_url}</code>
      </dd>
      {extra}
      <dt>{t("Models")}</dt>
      <dd>
        {row.models.length === 0 ? (
          <span className="muted">—</span>
        ) : (
          <div className="model-list">
            {row.models.map((m) => (
              <div key={m.id} className="model-line">
                <code>{m.id}</code>
                <span className="small muted">
                  {tokens(m.context_window)} {t("in")} · {tokens(m.max_output)} {t("out")}
                </span>
                <Abilities m={m} />
                <span className="small muted">
                  {(row.works_with[m.id] ?? []).length ? `${t("works with")} ${row.works_with[m.id].join(", ")}` : t("no agent can run it")}
                </span>
              </div>
            ))}
          </div>
        )}
      </dd>
    </dl>
  );
}

/** Add or change a model API. ``base`` is ``/api/settings/providers`` for the
 * service's, ``/api/me/providers`` for the person's own. */
export function ApiForm({
  base,
  initial,
  hasKey,
  service,
  onDone,
}: {
  base: string;
  initial?: ApiRow;
  hasKey: boolean;
  service: boolean;
  onDone: (approved: boolean) => void;
}) {
  const t = useT();
  const [id, setId] = useState(initial?.id ?? "");
  const [idTouched, setIdTouched] = useState(!!initial);
  const [name, setName] = useState(initial?.name ?? "");
  const [protocol, setProtocol] = useState<Protocol>(initial?.protocol ?? "openai");
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? "");
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState(initial?.api_key ?? "");
  const [models, setModels] = useState<ModelSpec[]>(initial?.models ?? []);
  const [adding, setAdding] = useState("");
  const [found, setFound] = useState<ModelList | null>(null);
  const [results, setResults] = useState<Record<string, Examination>>({});
  const [testing, setTesting] = useState<string[]>([]);
  const probe = (m?: string) => ({ protocol, base_url: baseUrl.trim(), key: key.trim() || null, provider: initial?.id ?? null, model: m ?? null });
  const query = useAct(() => api.post<ModelList>(`${base}/query`, probe()), []);
  const examine = useAct((m: string) => api.post<Examination>(`${base}/examine`, probe(m)), []);
  const save = useAct(
    () =>
      api.put<{ approved?: boolean }>(`${base}/${id}`, {
        name: name.trim() || null,
        protocol,
        base_url: baseUrl.trim(),
        models,
        key: key.trim() || null,
        ...(service ? { api_key: secret.trim() || null } : {}),
      }),
    service ? [["settings"], ["memory"]] : [["my-agents"]],
  );

  const setModel = (m: ModelSpec) => setModels((prev) => (prev.some((x) => x.id === m.id) ? prev.map((x) => (x.id === m.id ? m : x)) : [...prev, m]));
  const add = (ids: string[]) =>
    ids.forEach((mid) => {
      const listed = found?.models.find((x) => x.id === mid);
      if (!models.some((x) => x.id === mid)) setModel({ ...blankModel(mid), ...(listed ?? {}) });
    });
  const test = async (mid: string) => {
    setTesting((s) => [...s, mid]);
    try {
      const r = await examine.mutateAsync(mid);
      setResults((prev) => ({ ...prev, [mid]: r }));
      const current = models.find((x) => x.id === mid) ?? blankModel(mid);
      setModel({ ...current, ...r.spec, context_window: current.context_window ?? r.spec.context_window ?? null, max_output: current.max_output ?? r.spec.max_output ?? null } as ModelSpec);
    } finally {
      setTesting((s) => s.filter((x) => x !== mid));
    }
  };
  const preset = (label: string) => {
    const p = PRESETS.find((x) => x.label === label);
    if (!p) return;
    setProtocol(p.protocol);
    setBaseUrl(p.base_url);
    if (!name) setName(p.name);
    if (!idTouched) setId(slug(p.name));
  };
  const canProbe = !!baseUrl.trim() && /^https?:\/\//.test(baseUrl.trim());

  return (
    <form
      className="card stack api-form"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(undefined, { onSuccess: (r) => (setKey(""), onDone(r?.approved !== false)) });
      }}
    >
      {!initial && (
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Start from")}</span>
          <select defaultValue="" onChange={(e) => preset(e.target.value)}>
            <option value="">{t("Choose a known endpoint, or fill in your own")}</option>
            {PRESETS.map((p) => (
              <option key={p.label} value={p.label}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="api-form-grid">
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Name")}</span>
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (!idTouched && !initial) setId(slug(e.target.value));
            }}
            placeholder="My gateway"
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("ID")}</span>
          <input
            value={id}
            disabled={!!initial}
            onChange={(e) => (setId(e.target.value), setIdTouched(true))}
            placeholder="my-gateway"
            spellCheck={false}
            className="mono"
            title={t("Lowercase words joined by hyphens; agents name the provider by it")}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("API")}</span>
          <select value={protocol} onChange={(e) => setProtocol(e.target.value as Protocol)}>
            {(Object.keys(PROTOCOLS) as Protocol[]).map((p) => (
              <option key={p} value={p}>
                {PROTOCOLS[p]}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Base URL")}</span>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" spellCheck={false} className="mono" />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{hasKey ? t("API key (set; leave empty to keep it)") : t("API key")}</span>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" spellCheck={false} placeholder={hasKey ? "••••••••" : ""} />
        </label>
        {service && (
          <label className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Kept as")}</span>
            <input
              value={secret}
              onChange={(e) => setSecret(e.target.value.toUpperCase())}
              placeholder={id ? `${id.toUpperCase().replace(/-/g, "_")}_API_KEY` : "MY_GATEWAY_API_KEY"}
              spellCheck={false}
              className="mono"
              title={t("The secret's name in the settings; the key itself never goes there")}
            />
          </label>
        )}
      </div>

      <div className="stack" style={{ gap: 6 }}>
        <span className="row" style={{ gap: 6 }}>
          <strong className="small">{t("Models")}</strong>
          <span className="spacer" />
          <button type="button" className="sm" disabled={!canProbe || query.isPending} onClick={() => query.mutate(undefined, { onSuccess: setFound })} title={t("Read the endpoint's model list with the key; spends no tokens")}>
            <Icon name="search" size={13} />
            {query.isPending ? t("Asking…") : t("Find models")}
          </button>
          {models.length > 0 && (
            <button
              type="button"
              className="sm"
              disabled={!canProbe || testing.length > 0}
              onClick={() => void models.reduce((p, m) => p.then(() => test(m.id)), Promise.resolve())}
              title={t("Talk to every model to learn what it can do; spends a few thousand tokens each")}
            >
              <Icon name="activity" size={13} />
              {t("Test all")}
            </button>
          )}
        </span>
        {found && !found.ok && <div className="error small">{found.error}</div>}
        {found?.ok && (
          <FoundModels
            found={found}
            have={models.map((m) => m.id)}
            onAdd={(ids) => {
              add(ids);
              setFound(null);
            }}
          />
        )}
        {models.length === 0 && <span className="small muted">{t("Find the endpoint's models, or add one by its ID.")}</span>}
        {models.map((m) => (
          <div key={m.id} className="model-line">
            <code>{m.id}</code>
            <span className="small muted">
              {tokens(m.context_window)} {t("in")} · {tokens(m.max_output)} {t("out")}
            </span>
            <Abilities m={m} />
            <span className="row" style={{ gap: 4, marginLeft: "auto" }}>
              <button type="button" className="ghost sm" disabled={!canProbe || testing.includes(m.id)} onClick={() => void test(m.id)} title={t("Hold a short conversation with it, then try a tool, an image, reasoning, and streaming")}>
                {testing.includes(m.id) ? t("Testing…") : t("Test")}
              </button>
              <button type="button" className="ghost sm icon" aria-label={`${t("Remove")} ${m.id}`} onClick={() => setModels((prev) => prev.filter((x) => x.id !== m.id))}>
                <Icon name="x" size={13} />
              </button>
            </span>
            {results[m.id] && <Checks r={results[m.id]} />}
          </div>
        ))}
        <span className="row" style={{ gap: 6 }}>
          <input
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (adding.trim()) (add([adding.trim()]), setAdding(""));
              }
            }}
            placeholder={t("Model ID")}
            spellCheck={false}
            className="mono"
            style={{ flex: 1, minWidth: 0 }}
          />
          <button type="button" className="sm" disabled={!adding.trim()} onClick={() => (add([adding.trim()]), setAdding(""))}>
            <Icon name="plus" size={13} />
            {t("Add")}
          </button>
        </span>
      </div>

      <span className="row" style={{ gap: 6 }}>
        <button className="primary sm" disabled={!id || !baseUrl.trim() || save.isPending}>
          {save.isPending ? t("Saving…") : t("Save")}
        </button>
        <button type="button" className="ghost sm" onClick={() => onDone(true)}>
          {t("Cancel")}
        </button>
        <span className="small muted">{service ? t("The key goes to the service's secret store, never into the settings.") : t("Your key stays on this host; only your own agents use it, for what you ask.")}</span>
      </span>
      <ErrorBox error={save.error || query.error || examine.error} />
    </form>
  );
}

function FoundModels({ found, have, onAdd }: { found: ModelList; have: string[]; onAdd: (ids: string[]) => void }) {
  const t = useT();
  const [filter, setFilter] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const rows = found.models.filter((m) => !have.includes(m.id) && m.id.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="card stack" style={{ gap: 6, padding: 10, background: "var(--surface-2)" }}>
      <span className="row" style={{ gap: 6 }}>
        <span className="small">
          {found.models.length} {t("models at the endpoint")}
        </span>
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("Filter")} style={{ flex: 1, minWidth: 0 }} />
        <button type="button" className="primary sm" disabled={!picked.length} onClick={() => onAdd(picked)}>
          {t("Add")} {picked.length || ""}
        </button>
      </span>
      <div className="found-models">
        {rows.slice(0, 200).map((m) => (
          <label key={m.id} className="row small" style={{ gap: 6 }}>
            <input type="checkbox" checked={picked.includes(m.id)} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, m.id] : p.filter((x) => x !== m.id)))} />
            <code>{m.id}</code>
            {(m.context_window || m.max_output) && (
              <span className="muted">
                {tokens(m.context_window)} / {tokens(m.max_output)}
              </span>
            )}
          </label>
        ))}
        {rows.length === 0 && <span className="small muted">{t("No other models.")}</span>}
      </div>
    </div>
  );
}

function Checks({ r }: { r: Examination }) {
  const t = useT();
  return (
    <details className="small" style={{ flexBasis: "100%" }} open={!r.ok}>
      <summary className={r.ok ? "muted" : ""}>
        {r.ok ? `${t("Works with")} ${r.agents.join(", ") || t("no agent")}` : t("It did not hold a conversation on any API")}
      </summary>
      <ul className="checklist">
        {r.checks.map((c) => (
          <li key={c.name}>
            <span className={c.ok ? "check-ok" : "check-bad"}>
              <Icon name={c.ok ? "check" : "x"} size={12} />
            </span>
            <span>
              {c.name}: <span className="muted">{c.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** The key's state on a person's own API. */
export function KeyState({ set }: { set: boolean }) {
  const t = useT();
  return set ? <Status value="executed" label={t("key set")} /> : <Status value="pending" label={t("no key")} />;
}
