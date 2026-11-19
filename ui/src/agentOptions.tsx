// An agent CLI's settings (design 5.7, ``domain/agent_options.py``): its sandbox,
// network access, web search, and the like, as one form. The owner sets them for the
// service's accounts on Settings → Agents, and each person for their own account on
// My agents, where an option left unset takes the service's. Each run gets them as
// flags, inline settings, or a file of its own; the CLI's own configuration is never
// written, so nobody's choice reaches anyone else's runs.

import { useEffect, useState } from "react";
import { ErrorBox } from "./components";
import { useT } from "./i18n";

export interface AgentOption {
  key: string;
  label: string;
  help: string;
  kind: "bool" | "choice" | "number" | "text" | "list";
  /** What applies when nobody sets it, in words. */
  default: string;
  /** The default is the service's choice; ``cli_default`` is the CLI's own. */
  by_service: boolean;
  cli_default: string | null;
  choices: { value: string; label: string }[];
  minimum: number | null;
  maximum: number | null;
}

type Options = Record<string, unknown>;

/** A default said in words ("on", "off", "none") as the controls say it. */
const WORDS: Record<string, string> = { on: "On", off: "Off", none: "None" };

function shown(o: AgentOption, value: unknown, t: (s: string) => string): string {
  if (typeof value === "boolean") return value ? t("On") : t("Off");
  if (Array.isArray(value)) return value.length ? value.join(", ") : t("None");
  const choice = o.choices.find((c) => c.value === value);
  return choice ? t(choice.label) : t(WORDS[String(value)] ?? String(value));
}

/** What an unset option falls back to, said in full. */
function fallback(o: AgentOption, inherited: Options | undefined, t: (s: string) => string): string {
  if (inherited && inherited[o.key] !== undefined) return `${t("Service setting")}: ${shown(o, inherited[o.key], t)}`;
  const value = shown(o, o.default, t);
  if (o.by_service) return `${t("Service default")}: ${value} (${t("the CLI's own")}: ${shown(o, o.cli_default, t)})`;
  return `${t("CLI default")}: ${value}`;
}

export function AgentOptionsForm({
  schema,
  value,
  inherited,
  canChange,
  saving,
  error,
  onSave,
}: {
  schema: AgentOption[];
  value: Options;
  /** The service's settings, which a person's unset options take. */
  inherited?: Options;
  canChange: boolean;
  saving: boolean;
  error: unknown;
  onSave: (options: Options) => void;
}) {
  const t = useT();
  const [draft, setDraft] = useState<Options>(value);
  const key = JSON.stringify(value);
  useEffect(() => setDraft(JSON.parse(key) as Options), [key]);
  // A list is edited as lines of text, and becomes a list when saved.
  const saved = (d: Options): Options => {
    const out: Options = {};
    for (const [k, v] of Object.entries(d)) {
      const list = typeof v === "string" && schema.find((o) => o.key === k)?.kind === "list";
      const value = list
        ? (v as string)
            .split("\n")
            .map((x) => x.trim())
            .filter(Boolean)
        : v;
      if (!(Array.isArray(value) && !value.length)) out[k] = value;
    }
    return out;
  };
  const dirty = JSON.stringify(saved(draft)) !== key;
  const set = (k: string, v: unknown) => {
    const next = { ...draft };
    if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete next[k];
    else next[k] = v;
    setDraft(next);
  };
  if (!schema.length) return <span className="small muted">{t("This CLI has no settings beyond its model and reasoning level.")}</span>;
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="agent-options">
        {schema.map((o) => {
          const v = draft[o.key];
          const id = `opt-${o.key}`;
          const unset = fallback(o, inherited, t);
          return (
            <div key={o.key} className="agent-option">
              <label htmlFor={id} className="stack" style={{ gap: 2 }}>
                <span>{t(o.label)}</span>
                <span className="small muted">{t(o.help)}</span>
              </label>
              <div className="stack" style={{ gap: 2 }}>
                {o.kind === "bool" ? (
                  <select id={id} disabled={!canChange} value={v === undefined ? "" : v ? "on" : "off"} onChange={(e) => set(o.key, e.target.value === "" ? undefined : e.target.value === "on")}>
                    <option value="">{unset}</option>
                    <option value="on">{t("On")}</option>
                    <option value="off">{t("Off")}</option>
                  </select>
                ) : o.kind === "choice" ? (
                  <select id={id} disabled={!canChange} value={(v as string | undefined) ?? ""} onChange={(e) => set(o.key, e.target.value || undefined)}>
                    <option value="">{unset}</option>
                    {o.choices.map((c) => (
                      <option key={c.value} value={c.value}>
                        {t(c.label)}
                      </option>
                    ))}
                  </select>
                ) : o.kind === "number" ? (
                  <input
                    id={id}
                    type="number"
                    disabled={!canChange}
                    min={o.minimum ?? undefined}
                    max={o.maximum ?? undefined}
                    step="any"
                    value={v === undefined ? "" : String(v)}
                    placeholder={unset}
                    onChange={(e) => set(o.key, e.target.value === "" ? undefined : Number(e.target.value))}
                  />
                ) : o.kind === "list" ? (
                  <textarea
                    id={id}
                    rows={2}
                    disabled={!canChange}
                    value={Array.isArray(v) ? (v as string[]).join("\n") : typeof v === "string" ? v : ""}
                    placeholder={unset}
                    onChange={(e) => set(o.key, e.target.value || undefined)}
                  />
                ) : (
                  <input id={id} disabled={!canChange} value={(v as string | undefined) ?? ""} placeholder={unset} onChange={(e) => set(o.key, e.target.value || undefined)} />
                )}
              </div>
            </div>
          );
        })}
      </div>
      {canChange && dirty && (
        <div className="row" style={{ gap: 8 }}>
          <span className="spacer" />
          <button className="sm ghost" onClick={() => setDraft(JSON.parse(key) as Options)}>
            {t("Cancel")}
          </button>
          <button className="sm primary" disabled={saving} onClick={() => onSave(saved(draft))}>
            {t("Save")}
          </button>
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

/** How many options are set, for a summary line. */
export function setCount(value: Options | undefined, schema: AgentOption[]): number {
  return schema.filter((o) => value && value[o.key] !== undefined).length;
}
