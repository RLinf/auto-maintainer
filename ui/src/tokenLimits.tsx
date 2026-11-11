// Settings → Usage: each person's agent tokens as one bar split by kind of task, the
// service's own work in a section of its own, and the token limits. A person's tokens
// are those of the work they asked for or started on the dashboard or in a
// conversation; their limit counts what that work used on the service's own agent
// accounts and model APIs over a rolling day, week (7 days), and month (30 days),
// never their own accounts. The owner of the service sees everyone and sets the
// limits; operators see everyone; others see their own.

import { useQuery } from "@tanstack/react-query";
import { useState, type FocusEvent, type MouseEvent, type ReactNode } from "react";
import { api, type PeopleUsage, type PersonUsage, type TokenLimit, type TokenLimits, type UsagePart, type UsageRow } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar } from "./avatar";
import { Empty, ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

type T = (s: string) => string;

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

const total = (r: { input_tokens: number; output_tokens: number }) => r.input_tokens + r.output_tokens;
const cost = (usd: number | null) => (usd == null ? "—" : `$${usd.toFixed(usd >= 100 ? 0 : 2)}`);
const pct = (part: number, whole: number) => (whole ? `${Math.round((100 * part) / whole)}%` : "—");

/** What each task kind is called; any other shows its own name. */
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
  person_notes: "Preferences",
};

export function kindLabel(kind: string, t: T): string {
  return KINDS[kind] ? t(KINDS[kind]) : kind;
}

/** The bars' parts: kinds of task grouped so a bar has at most nine, in a fixed order
 * with a fixed color each (``--series-N``), so a group keeps its place and color
 * whoever's bar it is in. Kinds in no group go to "Other". */
const GROUPS: { id: string; label: string; kinds: string[] }[] = [
  { id: "talk", label: "Conversations", kinds: ["chat", "review_talk", "people_talk"] },
  { id: "review", label: "Reviews", kinds: ["review", "verify", "followup"] },
  { id: "fix", label: "Fixes", kinds: ["fix"] },
  { id: "comments", label: "Comment checks", kinds: ["comment_check"] },
  { id: "lab", label: "Lab tests", kinds: ["reproduce"] },
  { id: "ci", label: "CI diagnosis", kinds: ["ci_diagnose", "ci_root_cause"] },
  { id: "self", label: "Self-improvement", kinds: ["self_improve"] },
  { id: "score", label: "Scoring and triage", kinds: ["evaluate", "calibrate", "triage"] },
];
const OTHER = { id: "other", label: "Other", kinds: [] as string[] };
const ALL_GROUPS = [...GROUPS, OTHER];

function groupOf(kind: string) {
  return GROUPS.find((g) => g.kinds.includes(kind)) ?? OTHER;
}

function color(groupId: string): string {
  const i = GROUPS.findIndex((g) => g.id === groupId);
  return i < 0 ? "var(--series-other)" : `var(--series-${i + 1})`;
}

interface Segment {
  id: string;
  label: string;
  tokens: number;
  runs: number;
}

/** ``by_kind`` as the groups' segments, in the groups' order, empty ones left out. */
function segments(byKind: Record<string, UsagePart>, t: T): Segment[] {
  const sums: Record<string, Segment> = {};
  for (const [kind, part] of Object.entries(byKind)) {
    const g = groupOf(kind);
    const s = (sums[g.id] ??= { id: g.id, label: t(g.label), tokens: 0, runs: 0 });
    s.tokens += part.tokens;
    s.runs += part.runs;
  }
  return ALL_GROUPS.map((g) => sums[g.id]).filter((s): s is Segment => !!s && s.tokens > 0);
}

/** Several usage rows as one: a person's runs on the service's accounts and on their own. */
function combine(rows: UsageRow[]): UsageRow {
  const out: UsageRow = { login: rows[0]?.login ?? null, runs: 0, failed: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, cost_usd: null, seconds: 0, last_at: null, by_kind: {}, by_runtime: {}, by_repo: {}, by_day: {} };
  const addPart = (into: Record<string, UsagePart>, key: string, p: UsagePart) => {
    const q = (into[key] ??= { runs: 0, failed: 0, tokens: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, cost_usd: null });
    q.runs += p.runs;
    q.failed += p.failed;
    q.tokens += p.tokens;
    q.input_tokens += p.input_tokens;
    q.cached_input_tokens += p.cached_input_tokens ?? 0;
    q.output_tokens += p.output_tokens;
    if (p.cost_usd != null) q.cost_usd = (q.cost_usd ?? 0) + p.cost_usd;
  };
  for (const r of rows) {
    out.runs += r.runs;
    out.failed += r.failed;
    out.input_tokens += r.input_tokens;
    out.cached_input_tokens += r.cached_input_tokens ?? 0;
    out.output_tokens += r.output_tokens;
    out.seconds += r.seconds;
    if (r.cost_usd != null) out.cost_usd = (out.cost_usd ?? 0) + r.cost_usd;
    for (const key of ["by_kind", "by_runtime", "by_repo"] as const) for (const [k, p] of Object.entries(r[key] ?? {})) addPart(out[key], k, p);
    for (const [day, kinds] of Object.entries(r.by_day ?? {})) {
      const d = (out.by_day[day] ??= {});
      for (const [k, n] of Object.entries(kinds)) d[k] = (d[k] ?? 0) + n;
    }
  }
  return out;
}

/** The UTC days of the period, oldest first, as ``by_day`` names them. */
function daysOf(since: string, days: number): string[] {
  const start = new Date(since);
  const out: string[] = [];
  for (let i = 0; i <= days; i++) {
    const d = new Date(start.getTime() + i * 86_400_000);
    const iso = d.toISOString().slice(0, 10);
    if (out[out.length - 1] !== iso) out.push(iso);
  }
  return out;
}

/** The tip shown over a hovered or focused part of a chart. */
function useTip() {
  const [tip, setTip] = useState<{ x: number; text: string } | null>(null);
  const on = (text: string) => ({
    onMouseEnter: (e: MouseEvent<HTMLElement>) => setTip({ x: e.currentTarget.offsetLeft + e.currentTarget.offsetWidth / 2, text }),
    onFocus: (e: FocusEvent<HTMLElement>) => setTip({ x: e.currentTarget.offsetLeft + e.currentTarget.offsetWidth / 2, text }),
    onMouseLeave: () => setTip(null),
    onBlur: () => setTip(null),
    tabIndex: 0,
    "aria-label": text,
  });
  const view = tip ? (
    <span className="chart-tip" style={{ left: tip.x }} role="tooltip">
      {tip.text}
    </span>
  ) : null;
  return { on, view };
}

/** One person's tokens as a bar on a shared scale (``scale``: the longest bar's
 * total), split into the groups' parts. */
function StackedBar({ parts, scale, t }: { parts: Segment[]; scale: number; t: T }) {
  const { on, view } = useTip();
  const sum = parts.reduce((a, p) => a + p.tokens, 0);
  if (!sum) return <span className="ubar-track" />;
  return (
    <span className="ubar-track">
      <span className="ubar" style={{ width: `${Math.max(0.5, (100 * sum) / (scale || sum))}%` }}>
        {parts.map((p) => (
          <span key={p.id} style={{ flexGrow: p.tokens, ["--c" as string]: color(p.id) }} {...on(`${p.label}: ${short(p.tokens)} · ${pct(p.tokens, sum)} · ${p.runs} ${t("runs")}`)} />
        ))}
      </span>
      {view}
    </span>
  );
}

function Legend({ ids, t }: { ids: string[]; t: T }) {
  if (!ids.length) return null;
  return (
    <span className="chart-legend">
      {ALL_GROUPS.filter((g) => ids.includes(g.id)).map((g) => (
        <span key={g.id} className="chart-key">
          <i style={{ ["--c" as string]: color(g.id) }} />
          {t(g.label)}
        </span>
      ))}
    </span>
  );
}

/** Tokens per day of the period, each day's column split into the groups' parts. */
function DailyColumns({ row, days, height = 88, t }: { row: UsageRow; days: string[]; height?: number; t: T }) {
  const { on, view } = useTip();
  const totals = days.map((d) => Object.values(row.by_day?.[d] ?? {}).reduce((a, n) => a + n, 0));
  const peak = Math.max(0, ...totals);
  if (!peak) return null;
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="chart-cols" style={{ height }}>
        {days.map((d, i) => {
          const parts = segments(Object.fromEntries(Object.entries(row.by_day?.[d] ?? {}).map(([k, n]) => [k, { tokens: n, runs: 0 } as UsagePart])), t);
          const top = parts
            .slice()
            .sort((a, b) => b.tokens - a.tokens)
            .slice(0, 2)
            .map((p) => `${p.label} ${short(p.tokens)}`)
            .join(", ");
          return (
            <span key={d} className="chart-col" {...on(`${d}: ${short(totals[i])}${top ? ` (${top})` : ""}`)}>
              <span className="chart-col-fill" style={{ height: `${(100 * totals[i]) / peak}%` }}>
                {parts.map((p) => (
                  <span key={p.id} style={{ flexGrow: p.tokens, ["--c" as string]: color(p.id) }} />
                ))}
              </span>
            </span>
          );
        })}
        {view}
      </div>
      <span className="row small muted" style={{ justifyContent: "space-between" }}>
        <span>{days[0]}</span>
        <span>
          {t("peak")} {short(peak)}
          {t("/day")}
        </span>
        <span>{days[days.length - 1]} (UTC)</span>
      </span>
    </div>
  );
}

/** Parts of a row ranked largest first, one bar each on a shared scale. */
function RankedBars({ parts, label, t, extra }: { parts: Record<string, UsagePart>; label: (k: string) => ReactNode; t: T; extra?: (p: UsagePart) => ReactNode }) {
  const rows = Object.entries(parts).sort((a, b) => b[1].tokens - a[1].tokens);
  const max = rows[0]?.[1].tokens ?? 0;
  const sum = rows.reduce((a, [, p]) => a + p.tokens, 0);
  if (!rows.length) return <span className="small muted">{t("Nothing yet in this period.")}</span>;
  return (
    <div className="ranked">
      {rows.map(([k, p]) => (
        <div key={k} className="ranked-row" title={`${p.runs} ${t("runs")}${p.failed ? `, ${p.failed} ${t("failed")}` : ""}`}>
          <span className="ranked-label truncate">{label(k)}</span>
          <span className="ranked-track">
            <span style={{ width: `${max ? Math.max(0.5, (100 * p.tokens) / max) : 0}%` }} />
          </span>
          <span className="ranked-value">
            <strong>{short(p.tokens)}</strong> <span className="muted">{pct(p.tokens, sum)}</span>
          </span>
          {extra && <span className="ranked-extra small muted">{extra(p)}</span>}
        </div>
      ))}
    </div>
  );
}

/** Every task kind of a row, with what it read, from the cache, and wrote. */
function KindTable({ row, t }: { row: UsageRow; t: T }) {
  const kinds = Object.entries(row.by_kind).sort((a, b) => b[1].tokens - a[1].tokens);
  const sum = total(row);
  return (
    <div className="table-wrap">
      <table className="table usage-kinds">
        <thead>
          <tr>
            <th>{t("Task")}</th>
            <th className="num">{t("Runs")}</th>
            <th className="num" title={t("Input read fresh, not from the provider's prompt cache")}>
              {t("Fresh input")}
            </th>
            <th className="num" title={t("Input read from the provider's prompt cache, which costs a fraction of the rest")}>
              {t("Cached input")}
            </th>
            <th className="num">{t("Output")}</th>
            <th className="num">{t("Per run")}</th>
            <th className="num">{t("Cost")}</th>
            <th className="num">{t("Share")}</th>
          </tr>
        </thead>
        <tbody>
          {kinds.map(([k, p]) => (
            <tr key={k}>
              <td>
                <span className="chart-key">
                  <i style={{ ["--c" as string]: color(groupOf(k).id) }} />
                  {kindLabel(k, t)}
                </span>
              </td>
              <td className="num">
                {p.runs}
                {p.failed > 0 && <span className="muted"> ({p.failed} {t("failed")})</span>}
              </td>
              <td className="num">{short(p.input_tokens - (p.cached_input_tokens ?? 0))}</td>
              <td className="num">{p.cached_input_tokens ? short(p.cached_input_tokens) : <span className="muted">—</span>}</td>
              <td className="num">{short(p.output_tokens)}</td>
              <td className="num">{short(Math.round(p.tokens / Math.max(1, p.runs)))}</td>
              <td className="num">{cost(p.cost_usd)}</td>
              <td className="num">{pct(p.tokens, sum)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A row's parts as one line: ``antigravity 2.1M · claude-code 1.3M``. */
function PartsLine({ title, parts, label }: { title: string; parts: Record<string, UsagePart>; label: (k: string) => string }) {
  const rows = Object.entries(parts).sort((a, b) => b[1].tokens - a[1].tokens);
  if (!rows.length) return null;
  return (
    <span>
      <span className="muted">{title}:</span> {rows.map(([k, p]) => `${label(k)} ${short(p.tokens)}`).join(" · ")}
    </span>
  );
}

/** A person's limit, as a meter per window it sets. */
function LimitMeters({ p, t }: { p: PersonUsage; t: T }) {
  const set = WINDOWS.filter(([, key]) => p.limit[key] != null);
  if (!set.length) return <span className="small muted">{t("no limit")}</span>;
  return (
    <span className="limit-meters">
      {set.map(([w, key, label]) => {
        const cap = p.limit[key] as number;
        const used = p.used[w];
        const share = cap === 0 ? 1 : Math.min(1, used / cap);
        const c = share >= 1 ? "var(--bad)" : share >= 0.8 ? "var(--warn)" : "var(--ok)";
        return (
          <span key={w} className="limit-meter-row" title={`${used.toLocaleString()} / ${cap.toLocaleString()} ${t("tokens")}`}>
            <span className="small muted">{t(label)}</span>
            <span className="quality-meter" style={{ ["--qc" as string]: c }}>
              <span style={{ width: `${Math.round(share * 100)}%` }} />
            </span>
            <span className="small">
              {short(used)} <span className="muted">/ {short(cap)}</span>
            </span>
          </span>
        );
      })}
    </span>
  );
}

type Counted = "service" | "all";

function PersonBar({ p, row, scale, days, counted, limits, t }: { p: PersonUsage; row: UsageRow; scale: number; days: string[]; counted: Counted; limits: boolean; t: T }) {
  const aliasOf = useAliasOf();
  const [open, setOpen] = useState(false);
  const name = aliasOf(p.login) ?? p.login;
  const own = total(p.own);
  return (
    <div className={`ubar-person${open ? " open" : ""}`}>
      <div className={`ubar-row${limits ? " with-limits" : ""}`} onClick={() => setOpen(!open)}>
        <button type="button" className="ubar-who" aria-expanded={open} onClick={(e) => {
            e.stopPropagation();
            setOpen(!open);
          }}>
          <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
          <Avatar login={p.login} size={18} />
          <span className="truncate" title={p.login}>
            {name}
          </span>
        </button>
        <StackedBar parts={segments(row.by_kind, t)} scale={scale} t={t} />
        <span className="ubar-total">
          <strong>{short(total(row))}</strong>
          {counted === "service" && own > 0 && (
            <span className="small muted" title={t("Runs on their own agent accounts and model APIs, which no limit counts")}>
              +{short(own)} {t("own")}
            </span>
          )}
        </span>
        {limits && (
          <span className="ubar-limits">
            <LimitMeters p={p} t={t} />
          </span>
        )}
      </div>
      {open && (
        <div className="ubar-detail">
          <KindTable row={row} t={t} />
          <DailyColumns row={row} days={days} t={t} />
          <div className="usage-detail small">
            <span>
              <span className="muted">{t("Runs")}:</span> {row.runs}
              {row.failed > 0 && ` (${row.failed} ${t("failed")})`} · <span className="muted">{t("Cost")}:</span> {cost(row.cost_usd)}
            </span>
            <PartsLine title={t("By agent")} parts={row.by_runtime} label={(k) => k} />
            <PartsLine title={t("By repository")} parts={row.by_repo} label={(k) => k || t("No repository")} />
            {counted === "service" && own > 0 && <PartsLine title={t("On their own accounts")} parts={p.own.by_kind} label={(k) => kindLabel(k, t)} />}
            <span className="muted">{WINDOWS.some(([, key]) => p.limit[key] != null) ? (p.limit_own ? t("A limit of their own.") : t("The default limit.")) : t("No limit applies to them.")}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** Each person's tokens: one bar per person on a shared scale, split by kind of task. */
function PeopleSection({ u, days, setDays, t }: { u: PeopleUsage; days: number; setDays: (d: number) => void; t: T }) {
  const [counted, setCounted] = useState<Counted>("service");
  const rows = u.people.map((p) => ({ p, row: counted === "all" ? combine([p.service, p.own]) : p.service }));
  rows.sort((a, b) => total(b.row) - total(a.row));
  const scale = Math.max(0, ...rows.map((r) => total(r.row)));
  const present = [...new Set(rows.flatMap((r) => segments(r.row.by_kind, t).map((s) => s.id)))];
  const limits = u.people.some((p) => WINDOWS.some(([, key]) => p.limit[key] != null));
  const dayList = daysOf(u.since, days);
  return (
    <Section
      title={u.everything ? t("Tokens by person") : t("My tokens")}
      hint={t("The tokens of the work each person asked for or started, on the dashboard or in a conversation, by kind of task. Open a person for every task, day, agent, and repository.")}
      aside={
        <span className="row" style={{ gap: 8 }}>
          <span className="segmented" role="group" aria-label={t("Which accounts count")}>
            <button type="button" className={counted === "service" ? "on" : ""} onClick={() => setCounted("service")} title={t("What their token limits count")}>
              {t("Service's accounts")}
            </button>
            <button type="button" className={counted === "all" ? "on" : ""} onClick={() => setCounted("all")}>
              {t("With their own")}
            </button>
          </span>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label={t("Period")}>
            {PERIODS.map((d) => (
              <option key={d} value={d}>
                {t("Last")} {d} {d === 1 ? t("day") : t("days")}
              </option>
            ))}
          </select>
        </span>
      }
    >
      {rows.every((r) => total(r.row) === 0) ? (
        <Empty icon="cpu">{t("Nothing yet in this period.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <Legend ids={present} t={t} />
          <div className="ubar-rows">
            {rows.map(({ p, row }) => (
              <PersonBar key={p.login} p={p} row={row} scale={scale} days={dayList} counted={counted} limits={limits} t={t} />
            ))}
          </div>
          <span className="small muted">
            {t("Bars share one scale. Input includes what was read from the provider's prompt cache; that part was not recorded before 2026-11-11, when Antigravity's runs also left it out.")}
          </span>
        </div>
      )}
      {!u.complete && <span className="small muted">{t("Only the most recent runs are counted; pick a shorter period for exact totals.")}</span>}
    </Section>
  );
}

function Stat({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="usage-stat">
      <span className="small muted">{label}</span>
      <strong>{value}</strong>
      {note && <span className="small muted">{note}</span>}
    </div>
  );
}

/** The service's own work, which nobody asked for and nobody's limit counts: scans,
 * CI diagnosis, scoring, and the like. */
function ServiceSection({ row, since, days, t }: { row: UsageRow; since: string; days: number; t: T }) {
  const dayList = daysOf(since, days);
  const sum = total(row);
  return (
    <Section title={t("The service's own work")} hint={t("Work the service started by itself, such as scans, CI diagnosis, and scoring. Nobody's limit counts it.")}>
      {row.runs === 0 ? (
        <Empty icon="cpu">{t("Nothing yet in this period.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          <div className="usage-stats">
            <Stat label={t("Tokens")} value={short(sum)} note={`${short(Math.round(sum / Math.max(1, days)))}${t("/day")}`} />
            <Stat label={t("Runs")} value={row.runs} note={row.failed ? `${row.failed} ${t("failed")} (${pct(row.failed, row.runs)})` : undefined} />
            <Stat label={t("Read from the cache")} value={row.cached_input_tokens ? pct(row.cached_input_tokens, row.input_tokens) : "—"} note={t("of input")} />
            <Stat label={t("Cost")} value={cost(row.cost_usd)} note={t("where the agent reports it")} />
          </div>
          <div className="stack" style={{ gap: 6 }}>
            <strong className="small">{t("By day")}</strong>
            <Legend ids={segments(row.by_kind, t).map((s) => s.id)} t={t} />
            <DailyColumns row={row} days={dayList} height={120} t={t} />
          </div>
          <div className="usage-ranked-grid">
            <div className="stack" style={{ gap: 6 }}>
              <strong className="small">{t("By task")}</strong>
              <RankedBars parts={row.by_kind} label={(k) => kindLabel(k, t)} t={t} extra={(p) => `${p.runs} × ${short(Math.round(p.tokens / Math.max(1, p.runs)))}`} />
            </div>
            <div className="stack" style={{ gap: 6 }}>
              <strong className="small">{t("By repository")}</strong>
              <RankedBars parts={row.by_repo} label={(k) => <span className="mono">{k || t("No repository")}</span>} t={t} />
              <strong className="small" style={{ marginTop: 10 }}>
                {t("By agent")}
              </strong>
              <RankedBars parts={row.by_runtime} label={(k) => <span className="mono">{k}</span>} t={t} />
            </div>
          </div>
          <span className="small muted">{t("Each task's runs × tokens per run. Open its runs on the Agents page.")}</span>
        </div>
      )}
    </Section>
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

/** Settings → Usage: each person's tokens, the service's own, and the limits. */
export function PeopleUsageTab() {
  const t = useT();
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["people-usage", days], queryFn: () => api.get<PeopleUsage>(`/api/people/usage?days=${days}`) });
  if (q.isLoading) return <Skeleton rows={4} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const u = q.data;
  return (
    <div className="stack" style={{ gap: 16 }}>
      <PeopleSection u={u} days={days} setDays={setDays} t={t} />
      {u.background && <ServiceSection row={u.background} since={u.since} days={days} t={t} />}
      <LimitsEditor key={JSON.stringify(u.limits)} limits={u.limits} canManage={u.can_manage} />
    </div>
  );
}
