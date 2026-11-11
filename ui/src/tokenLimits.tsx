// Each person's agent tokens and their token limits (Settings → Usage). A limit counts
// the tokens a person's requests use on the service's own agent accounts and model
// APIs, over a rolling day, week (7 days), and month (30 days); their own accounts
// never count. The owner of the service sees everyone and sets the limits; others
// see their own.

import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api, type PeopleUsage, type PersonUsage, type TokenLimit, type TokenLimits, type UsageRow } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar } from "./avatar";
import { ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

const PERIODS = [1, 7, 30, 90];
const WINDOWS = [
  ["day", "daily_tokens", "Per day"],
  ["week", "weekly_tokens", "Per week"],
  ["month", "monthly_tokens", "Per month"],
] as const;

/** ``n`` tokens, short: 1.2M, 35k. */
export function short(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** A number of tokens as people type it: 2M, 500k, 1.5m, 1000000; blank is none. */
export function parseTokens(text: string): number | null | undefined {
  const s = text.trim().toLowerCase().replace(/[,_\s]/g, "");
  if (!s) return null;
  const m = s.match(/^(\d+(?:\.\d+)?)([km]?)$/);
  if (!m) return undefined;
  const n = Number(m[1]) * (m[2] === "m" ? 1_000_000 : m[2] === "k" ? 1_000 : 1);
  return Number.isFinite(n) ? Math.round(n) : undefined;
}

const total = (r: UsageRow) => r.input_tokens + r.output_tokens;

function Meter({ used, cap }: { used: number; cap: number | null | undefined }) {
  const t = useT();
  if (cap == null) return <span className="small muted">{short(used)}</span>;
  const share = cap === 0 ? 1 : Math.min(1, used / cap);
  const color = share >= 1 ? "var(--bad)" : share >= 0.8 ? "var(--warn)" : "var(--ok)";
  return (
    <span className="stack" style={{ gap: 3, minWidth: 0 }} title={`${used.toLocaleString()} / ${cap.toLocaleString()} ${t("tokens")}`}>
      <span className="small">
        {short(used)} <span className="muted">/ {short(cap)}</span>
      </span>
      <span className="quality-meter" style={{ ["--qc" as string]: color }}>
        <span style={{ width: `${Math.round(share * 100)}%` }} />
      </span>
    </span>
  );
}

/** What each task kind is called in the usage table; any other shows its own name. */
const KINDS: Record<string, string> = {
  chat: "Chat",
  review: "Reviews",
  followup: "Follow-ups",
  verify: "Verification",
  review_talk: "Review discussions",
  fix: "Fixes",
  reproduce: "Reproductions",
  comment_check: "Comment checks",
  evaluate: "Priority scoring",
  calibrate: "Calibration",
  ci_diagnose: "CI diagnosis",
  ci_root_cause: "CI root cause",
  triage: "Triage",
  hygiene: "Hygiene",
  translate: "Translation",
  governance: "People record",
  people_talk: "People talk",
  onboard: "Onboarding",
  learn: "Lessons",
  self_improve: "Self-improvement",
};

export function kindLabel(kind: string, t: (s: string) => string): string {
  return KINDS[kind] ? t(KINDS[kind]) : kind;
}

const COLUMNS = 5;
/** The column of the task kinds past the first ``COLUMNS``. */
const OTHER = "__other__";

/** The task kinds across ``rows``, those with the most tokens first: ``COLUMNS`` of
 * them, then ``OTHER`` for the rest. */
function kindsOf(rows: UsageRow[]): string[] {
  const sums: Record<string, number> = {};
  for (const r of rows) for (const [k, v] of Object.entries(r.by_kind)) sums[k] = (sums[k] ?? 0) + v.tokens;
  const kinds = Object.keys(sums).sort((a, b) => sums[b] - sums[a]);
  return kinds.length > COLUMNS + 1 ? [...kinds.slice(0, COLUMNS), OTHER] : kinds;
}

function KindCell({ row, kind, shown }: { row: UsageRow; kind: string; shown: string[] }) {
  const part =
    kind === OTHER
      ? Object.entries(row.by_kind)
          .filter(([k]) => !shown.includes(k))
          .reduce<{ runs: number; tokens: number } | undefined>((acc, [, v]) => ({ runs: (acc?.runs ?? 0) + v.runs, tokens: (acc?.tokens ?? 0) + v.tokens }), undefined)
      : row.by_kind[kind];
  if (!part) return <td className="num muted">—</td>;
  return (
    <td className="num" title={`${part.runs} runs`}>
      {short(part.tokens)}
    </td>
  );
}

/** A person's limit, as a meter per window it sets; none set is no limit. */
function LimitCell({ p }: { p: PersonUsage }) {
  const t = useT();
  const set = WINDOWS.filter(([, key]) => p.limit[key] != null);
  if (!set.length) return <span className="small muted">{t("no limit")}</span>;
  return (
    <span className="limit-meters">
      {set.map(([w, key, label]) => (
        <span key={w} className="limit-meter-row">
          <span className="small muted">{t(label)}</span>
          <Meter used={p.used[w]} cap={p.limit[key]} />
        </span>
      ))}
    </span>
  );
}

function PersonRow({ p, kinds, days }: { p: PersonUsage; kinds: string[]; days: number }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const [open, setOpen] = useState(false);
  const name = aliasOf(p.login) ?? p.login;
  const agents = Object.entries(p.service.by_runtime).sort((a, b) => b[1].tokens - a[1].tokens);
  const own = Object.entries(p.own.by_kind).sort((a, b) => b[1].tokens - a[1].tokens);
  return (
    <>
      <tr className="expandable" onClick={() => setOpen(!open)} aria-expanded={open}>
        <td>
          <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
            <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
            <Avatar login={p.login} size={18} />
            <span className="truncate" title={p.login}>
              {name}
            </span>
          </span>
        </td>
        <td className="num">
          <strong>{short(total(p.service))}</strong>
        </td>
        {kinds.map((k) => (
          <KindCell key={k} row={p.service} kind={k} shown={kinds} />
        ))}
        <td>
          <LimitCell p={p} />
        </td>
        <td className="num">{p.own.runs ? short(total(p.own)) : <span className="muted">—</span>}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={kinds.length + 4}>
            <div className="usage-detail small">
              <span>
                <span className="muted">{t("Runs")}:</span> {p.service.runs}
                {p.service.failed > 0 && ` (${p.service.failed} ${t("failed")})`} · <span className="muted">{t("in")}</span> {short(p.service.input_tokens)} ·{" "}
                <span className="muted">{t("out")}</span> {short(p.service.output_tokens)}
                {p.service.cost_usd != null && ` · $${p.service.cost_usd.toFixed(2)}`}
              </span>
              <span>
                <span className="muted">{t("By kind")}:</span>{" "}
                {Object.entries(p.service.by_kind)
                  .sort((a, b) => b[1].tokens - a[1].tokens)
                  .map(([k, v]) => `${kindLabel(k, t)} ${short(v.tokens)} (${v.runs})`)
                  .join(" · ")}
              </span>
              {agents.length > 0 && (
                <span>
                  <span className="muted">{t("By agent")}:</span> {agents.map(([n, v]) => `${n} ${short(v.tokens)} (${v.runs})`).join(" · ")}
                </span>
              )}
              {own.length > 0 && (
                <span>
                  <span className="muted">
                    {t("On their own accounts, last")} {days} {t("days")}:
                  </span>{" "}
                  {own.map(([k, v]) => `${kindLabel(k, t)} ${short(v.tokens)}`).join(" · ")}
                </span>
              )}
              <span className="muted">{p.limit_own ? t("A limit of their own.") : t("The default limit.")}</span>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** One limit's three windows, as inputs; blank is no limit. */
function LimitInputs({ value, onChange, disabled }: { value: TokenLimit; onChange: (v: TokenLimit) => void; disabled: boolean }) {
  const t = useT();
  const [text, setText] = useState<Record<string, string>>({});
  return (
    <span className="limit-inputs">
      {WINDOWS.map(([w, key, label]) => {
        const shown = text[key] ?? (value[key] != null ? short(value[key] as number) : "");
        const parsed = parseTokens(shown);
        return (
          <label key={w} className="stack" style={{ gap: 2 }}>
            <span className="small muted">{t(label)}</span>
            <input
              value={shown}
              disabled={disabled}
              placeholder={t("no limit")}
              aria-invalid={parsed === undefined}
              onChange={(e) => {
                setText({ ...text, [key]: e.target.value });
                const n = parseTokens(e.target.value);
                if (n !== undefined) onChange({ ...value, [key]: n });
              }}
            />
          </label>
        );
      })}
    </span>
  );
}

/** The default limit and each person's own, which the owner of the service edits. */
function LimitsEditor({ limits, canManage }: { limits: TokenLimits; canManage: boolean }) {
  const t = useT();
  const toast = useToast();
  const aliasOf = useAliasOf();
  const [draft, setDraft] = useState<TokenLimits>(limits);
  const [login, setLogin] = useState("");
  const [key, setKey] = useState(0);
  const save = useAct((l: TokenLimits) => api.put<{ approved: boolean }>("/api/settings/token-limits", { limits: l }), [["people-usage"], ["settings"], ["memory"]]);
  const changed = JSON.stringify(draft) !== JSON.stringify(limits);
  const people = Object.entries(draft.people);
  return (
    <Section
      title={t("Token limits")}
      hint={t("What each person's requests may use on the service's agents and model APIs; their own agent accounts never count")}
      aside={
        canManage && changed ? (
          <span className="row" style={{ gap: 6 }}>
            <button
              className="ghost sm"
              onClick={() => {
                setDraft(limits);
                setKey(key + 1);
              }}
            >
              {t("Cancel")}
            </button>
            <button
              className="primary sm"
              disabled={save.isPending}
              onClick={() =>
                save.mutate(draft, {
                  onSuccess: (r) => toast({ title: r.approved ? t("Saved") : t("Change proposed"), body: t("It applies to the next run."), icon: r.approved ? "check" : "inbox" }),
                })
              }
            >
              {t("Save")}
            </button>
          </span>
        ) : undefined
      }
    >
      <div className="stack" style={{ gap: 12 }} key={key}>
        <div className="limit-row">
          <span className="limit-who">
            <strong>{t("Everyone")}</strong>
            <span className="small muted">{t("unless a person has a limit of their own")}</span>
          </span>
          <LimitInputs value={draft.default} disabled={!canManage} onChange={(v) => setDraft({ ...draft, default: v })} />
        </div>
        {people.map(([who, limit]) => (
          <div key={who} className="limit-row">
            <span className="limit-who limit-person">
              <Avatar login={who} size={18} />
              <strong className="truncate">{aliasOf(who) ?? who}</strong>
              {!limit.daily_tokens && !limit.weekly_tokens && !limit.monthly_tokens && <span className="pill status-info">{t("no limit")}</span>}
            </span>
            <LimitInputs value={limit} disabled={!canManage} onChange={(v) => setDraft({ ...draft, people: { ...draft.people, [who]: v } })} />
            {canManage && (
              <button
                className="ghost icon"
                aria-label={`${t("Remove")} ${who}`}
                title={t("Back to the default limit")}
                onClick={() => {
                  const rest = { ...draft.people };
                  delete rest[who];
                  setDraft({ ...draft, people: rest });
                }}
              >
                <Icon name="x" size={14} />
              </button>
            )}
          </div>
        ))}
        {canManage && (
          <form
            className="row"
            style={{ gap: 6 }}
            onSubmit={(e) => {
              e.preventDefault();
              const who = login.trim().replace(/^@/, "");
              if (!who || draft.people[who]) return;
              setDraft({ ...draft, people: { ...draft.people, [who]: {} } });
              setLogin("");
            }}
          >
            <input value={login} onChange={(e) => setLogin(e.target.value)} placeholder={t("GitHub login")} spellCheck={false} className="mono" style={{ maxWidth: 220 }} />
            <button className="sm" disabled={!login.trim()}>
              <Icon name="plus" size={13} />
              {t("A limit for one person")}
            </button>
          </form>
        )}
        <span className="small muted">{t("Rolling windows: the last 24 hours, 7 days, and 30 days. A run that starts under the limit finishes; past it, a request is refused until older runs leave the window, while the person's own agent accounts still run.")}</span>
        <ErrorBox error={save.error} />
      </div>
    </Section>
  );
}

/** Settings → Usage: the limits, and each person's tokens. */
export function PeopleUsageTab() {
  const t = useT();
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["people-usage", days], queryFn: () => api.get<PeopleUsage>(`/api/people/usage?days=${days}`) });
  if (q.isLoading) return <Skeleton rows={4} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const u = q.data;
  const kinds = kindsOf(u.people.map((p) => p.service));
  return (
    <div className="stack" style={{ gap: 16 }}>
      <LimitsEditor key={JSON.stringify(u.limits)} limits={u.limits} canManage={u.can_manage} />
      <Section
        title={u.everything ? t("Tokens by person") : t("My tokens")}
        hint={t("Tokens of the runs each person asked for on the service's agents, by kind of task; open a row for more")}
        aside={
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label={t("Period")}>
            {PERIODS.map((d) => (
              <option key={d} value={d}>
                {t("Last")} {d} {d === 1 ? t("day") : t("days")}
              </option>
            ))}
          </select>
        }
      >
        <div className="table-wrap">
          <table className="table usage-people">
            <thead>
              <tr>
                <th>{t("Person")}</th>
                <th className="num" title={t("Every run they asked for on the service's agents and model APIs")}>
                  {t("Total")}
                </th>
                {kinds.map((k) => (
                  <th key={k} className="num">
                    {k === OTHER ? t("Other") : kindLabel(k, t)}
                  </th>
                ))}
                <th>{t("Limit")}</th>
                <th className="num" title={t("Runs on their own agent accounts and model APIs, which no limit counts")}>
                  {t("Own accounts")}
                </th>
              </tr>
            </thead>
            <tbody>
              {u.people.map((p) => (
                <Fragment key={p.login}>
                  <PersonRow p={p} kinds={kinds} days={days} />
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {u.background && u.background.runs > 0 && (
          <div className="small" style={{ marginTop: 12 }}>
            <span className="muted">{t("The service's own work, which nobody's limit counts")}:</span> <strong>{short(total(u.background))}</strong> ·{" "}
            {Object.entries(u.background.by_kind)
              .sort((a, b) => b[1].tokens - a[1].tokens)
              .map(([k, v]) => `${kindLabel(k, t)} ${short(v.tokens)}`)
              .join(" · ")}
          </div>
        )}
        {!u.complete && <span className="small muted">{t("Only the most recent runs are counted; pick a shorter period for exact totals.")}</span>}
      </Section>
    </div>
  );
}
