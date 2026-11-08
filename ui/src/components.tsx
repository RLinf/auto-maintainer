// Shared display pieces: page headers, cards, status pills (icon or dot plus text, never
// color alone), times, diffs, empty and error states, and mutation helpers.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, type QualityBand, type QuickQuality, type ReviewQuality } from "./api";
import { REVIEW_STATES } from "./data";
import { currentLang, useLang, useT, type Lang } from "./i18n";
import { Icon, type IconName } from "./icons";
import { Skeleton } from "./ui";

export function PageHead({
  title,
  sub,
  actions,
  crumbs,
}: {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  crumbs?: { to: string; label: string }[];
}) {
  return (
    <div className="page-head">
      <div className="titles">
        {crumbs && (
          <nav className="crumbs" aria-label="Breadcrumb">
            {crumbs.map((c) => (
              <span key={c.to} className="row" style={{ gap: 6 }}>
                <Link to={c.to}>{c.label}</Link>
                <Icon name="chevronRight" size={12} />
              </span>
            ))}
          </nav>
        )}
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </div>
  );
}

export function Section({
  title,
  children,
  aside,
  hint,
  flush,
}: {
  title: ReactNode;
  children: ReactNode;
  aside?: ReactNode;
  hint?: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className={`card ${flush ? "flush" : ""}`}>
      <header className="card-head" style={flush ? { padding: "14px 18px 0" } : undefined}>
        <div className="row" style={{ gap: 8 }}>
          <h2>{title}</h2>
          {hint && <span className="hint">{hint}</span>}
        </div>
        {aside}
      </header>
      {children}
    </section>
  );
}

export function Tier({ tier }: { tier: string | null | undefined }) {
  if (!tier) return <span className="muted" title="Not evaluated">—</span>;
  return <span className={`pill tier tier-${tier.toLowerCase()}`}>{tier}</span>;
}

export const QUALITY_BANDS: { band: QualityBand; floor: number; label: string }[] = [
  { band: "excellent", floor: 90, label: "Excellent" },
  { band: "good", floor: 75, label: "Good" },
  { band: "fair", floor: 60, label: "Fair" },
  { band: "poor", floor: 40, label: "Poor" },
  { band: "bad", floor: 0, label: "Bad" },
];
export const qualityLabel = (band: QualityBand) => QUALITY_BANDS.find((b) => b.band === band)?.label ?? band;

/** The agent's reason for a score in the dashboard's language, else as it was given
 *  (Markdown). */
export function reasonIn(q: { reason: string; reasons?: Record<string, string> }, lang: Lang): string {
  return q.reasons?.[lang] || q.reason;
}

/** The basics an agent checks before scoring, in order (playbooks/prompts.py: QUALITY_CHECKS). */
export const QUALITY_CHECKS: [string, string][] = [
  ["requirements", "Requirements"],
  ["tests", "Tests"],
  ["docs", "Docs"],
  ["style", "Style"],
  ["design", "Design"],
];

/** The basics the agent checked, each met (✓), not met (✗), or not applicable (—). */
export function QualityChecks({ checks }: { checks?: Record<string, boolean | null> }) {
  const t = useT();
  if (!checks || !Object.keys(checks).length) return null;
  return (
    <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>
      {QUALITY_CHECKS.filter(([area]) => area in checks).map(([area, label]) => {
        const ok = checks[area];
        return (
          <span
            key={area}
            className={`pill ${ok === true ? "status-ok" : ok === false ? "status-bad" : ""}`}
            title={ok === true ? t("met") : ok === false ? t("not met") : t("does not apply")}
          >
            {ok === null ? <span aria-hidden>—</span> : <Icon name={ok ? "check" : "x"} size={12} />}
            {t(label)}
          </span>
        );
      })}
    </span>
  );
}

/** The checks a score's PR missed, for a tooltip. */
export function missedChecks(checks: Record<string, boolean | null> | undefined, t: (s: string) => string): string {
  const missed = QUALITY_CHECKS.filter(([area]) => checks?.[area] === false).map(([, label]) => t(label));
  return missed.length ? `✗ ${missed.join(" · ")}` : "";
}

/** Markdown as plain text, for a tooltip: no emphasis marks, code ticks, or link URLs. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`]+/g, "")
    .replace(/^\s*[-+]\s+/gm, "• ")
    .replace(/^#+\s*/gm, "")
    .trim();
}

/** Whether a score was given at an older commit than ``head``. */
export function qualityStale(q: { sha: string }, head: string | null | undefined): boolean {
  if (!head || !q.sha) return false;
  const n = Math.min(head.length, q.sha.length);
  return q.sha.slice(0, n) !== head.slice(0, n);
}

/** Whether a PR shows its quick score: it has no review score, or new commits came
 *  after its review and the quick score covers them. */
export function showsQuick(quality: ReviewQuality | null | undefined, quick: QuickQuality | null | undefined, head?: string | null): boolean {
  if (!quick) return false;
  if (!quality) return true;
  return !!head && qualityStale(quality, head) && !qualityStale(quick, head);
}

/** The score a PR shows: the review's (accurate) one, or the quick one before a
 *  review or after new commits (``showsQuick``). */
export function shownQuality(
  quality: ReviewQuality | null | undefined,
  quick: QuickQuality | null | undefined,
  head?: string | null,
): { score: number; band: QualityBand } | null {
  return showsQuick(quality, quick, head) ? (quick ?? null) : (quality ?? quick ?? null);
}

/** A PR's quality as a colored pill: the review's score, or else the quick score at a
 *  glance (dashed); the score, its band unless ``compact``, and the agent's reason on
 *  hover. Faded when new commits arrived since it was given. */
export function Quality({
  quality,
  quick,
  head,
  compact,
}: {
  quality: ReviewQuality | null | undefined;
  quick?: QuickQuality | null;
  head?: string | null;
  compact?: boolean;
}) {
  const t = useT();
  const { lang } = useLang();
  const isQuick = showsQuick(quality, quick, head);
  const shown = isQuick ? quick : (quality ?? quick);
  if (!shown) return <span className="muted" title={t("Not scored yet")}>—</span>;
  const stale = qualityStale(shown, head);
  const where =
    quality && !isQuick
      ? `${t(quality.kind === "followup" ? "follow-up at" : "review at")} ${quality.sha.slice(0, 7)}${quality.posted ? "" : ` (${t("not posted")})`}`
      : `${t("quick score from the diff at")} ${shown.sha.slice(0, 7)}`;
  const title = [
    `${isQuick ? t("Quick score") : t("Quality")} ${shown.score}/100 · ${t(qualityLabel(shown.band))}`,
    plainText(reasonIn(shown, lang)),
    missedChecks(shown.checks, t),
    where,
    isQuick && quality ? `${t("After review it scored")} ${quality.score} ${t("at")} ${quality.sha.slice(0, 7)}` : "",
    isQuick ? t("A review gives the accurate score.") : "",
    stale ? t("new commits since") : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className={`pill quality q-${shown.band}${isQuick ? " quick" : ""}${stale ? " stale" : ""}`} title={title} aria-label={title}>
      <span className="dot" />
      {shown.score}
      {!compact && <span className="q-label">{t(qualityLabel(shown.band))}</span>}
    </span>
  );
}

const STATUS_CLASS: Record<string, string> = {
  executed: "ok",
  approved: "ok",
  allowed: "ok",
  merged: "ok",
  done: "ok",
  passing: "ok",
  success: "ok",
  completed: "ok",
  neutral: "muted",
  skipped: "muted",
  stale: "muted",
  in_progress: "info",
  failure: "bad",
  timed_out: "bad",
  action_required: "warn",
  startup_failure: "bad",
  noop: "muted",
  shadowed: "muted",
  superseded: "muted",
  expired: "muted",
  cancelled: "muted",
  proposed: "warn",
  pending: "warn",
  waiting: "warn",
  queued: "info",
  running: "info",
  intent: "info",
  open: "info",
  failed: "bad",
  denied: "bad",
  rejected: "bad",
  conflict: "bad",
  flaky: "warn",
  infra: "info",
  pr_caused: "bad",
};

export function Status({ value, label }: { value: string | null | undefined; label?: string }) {
  const t = useT();
  if (!value) return null;
  const cls = STATUS_CLASS[value.toLowerCase()] ?? "info";
  return (
    <span className={`pill status-${cls}`}>
      <span className="dot" aria-hidden="true" />
      {label ?? t(value.replace(/_/g, " "))}
    </span>
  );
}

export function ReviewState({ state }: { state: string }) {
  const t = useT();
  const cls: Record<string, string> = {
    NEW_REVIEW: "warn",
    FOLLOWUP_REVIEW: "warn",
    APPROVED_NEW_COMMITS: "info",
    UP_TO_DATE: "ok",
    SELF_PR: "muted",
    DRAFT: "muted",
    CLOSED: "muted",
  };
  return (
    <span className={`pill status-${cls[state] ?? "info"}`} title={state}>
      <span className="dot" aria-hidden="true" />
      {t(REVIEW_STATES[state] ?? state)}
    </span>
  );
}

export function Labels({ labels, max = 4 }: { labels: string[]; max?: number }) {
  if (!labels.length) return <span className="muted">—</span>;
  const shown = labels.slice(0, max);
  return (
    <span className="labels">
      {shown.map((l) => (
        <span key={l} className="label">
          {l}
        </span>
      ))}
      {labels.length > max && (
        <span className="label" title={labels.slice(max).join(", ")}>
          +{labels.length - max}
        </span>
      )}
    </span>
  );
}

export function Loading({ rows = 5 }: { rows?: number }) {
  return <Skeleton rows={rows} />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? ` (${error.status})` : "";
  return (
    <div className="error" role="alert">
      {message}
      {status}
    </div>
  );
}

export function Empty({ children, icon = "inbox" }: { children?: ReactNode; icon?: IconName }) {
  return (
    <div className="empty">
      <Icon name={icon} size={22} />
      <div>{children ?? "Nothing here."}</div>
    </div>
  );
}

export function CaughtUp({ children }: { children: ReactNode }) {
  return (
    <div className="caught-up">
      <span className="big">
        <Icon name="check" size={22} />
      </span>
      <strong style={{ color: "var(--text)" }}>{children}</strong>
    </div>
  );
}

export function Diff({ text }: { text: string | null | undefined }) {
  if (!text) return <Empty icon="check">No differences.</Empty>;
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => {
        const cls =
          line.startsWith("+++") || line.startsWith("---")
            ? "diff-file"
            : line.startsWith("@@")
              ? "diff-hunk"
              : line.startsWith("+")
                ? "diff-add"
                : line.startsWith("-")
                  ? "diff-del"
                  : "";
        return (
          <div key={i} className={cls}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

export function Pre({ text }: { text: string }) {
  return <pre className="pre">{text}</pre>;
}

// ---- time ------------------------------------------------------------------------------

/** Re-render once a minute so relative times stay current. */
function useMinute() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 60_000);
    return () => window.clearInterval(id);
  }, []);
}

export function relative(at: string | Date, now = Date.now(), lang: Lang = currentLang()): string {
  const d = typeof at === "string" ? new Date(at) : at;
  const diff = (now - d.getTime()) / 1000;
  const future = diff < 0;
  const s = Math.abs(diff);
  const zh = lang === "zh";
  const units: Record<string, string> = zh ? { m: " 分钟", h: " 小时", d: " 天" } : { m: "m", h: "h", d: "d" };
  const fmt = (n: number, unit: string) =>
    zh ? `${n}${units[unit]}${future ? "后" : "前"}` : future ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (s < 45) return zh ? (future ? "马上" : "刚刚") : future ? "in a moment" : "just now";
  if (s < 3600) return fmt(Math.round(s / 60), "m");
  if (s < 86400) return fmt(Math.round(s / 3600), "h");
  if (s < 86400 * 30) return fmt(Math.round(s / 86400), "d");
  return d.toLocaleDateString(zh ? "zh-CN" : undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function When({ at, absolute }: { at: string | null | undefined; absolute?: boolean }) {
  useMinute();
  useLang();
  if (!at) return <span className="muted">—</span>;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return <span>{at}</span>;
  const full = d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return (
    <time dateTime={at} title={full}>
      {absolute ? full : relative(d)}
    </time>
  );
}

export function timeOf(at: string | null | undefined): number | null {
  if (!at) return null;
  const n = new Date(at).getTime();
  return Number.isNaN(n) ? null : n;
}

// ---- mutations -----------------------------------------------------------------------------

/** A mutation that invalidates the given query key prefixes when it succeeds. */
export function useAct<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>, invalidate: string[][]) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => invalidate.forEach((queryKey) => client.invalidateQueries({ queryKey })),
  });
}

export function prPath(repo: string, number: number | null | undefined): string {
  return `/pr/${repo}/${number ?? ""}`;
}

/** A reason prompt inline: the first click reveals the field, the second confirms. */
export function RejectButton({
  onReject,
  busy,
  label = "Reject",
  size,
  quiet,
}: {
  onReject: (reason: string) => void;
  busy?: boolean;
  label?: string;
  size?: "sm";
  /** A plain text button, for cards whose main action is something else. */
  quiet?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  if (!open) {
    return (
      <button className={`${quiet ? "ghost danger-text" : "danger"} ${size ?? ""}`} onClick={() => setOpen(true)}>
        {t(label)}
      </button>
    );
  }
  return (
    <form
      className="row"
      onSubmit={(e) => {
        e.preventDefault();
        if (reason.trim()) onReject(reason.trim());
      }}
    >
      <input autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Reason (required)")} style={size ? { height: 28 } : undefined} />
      <button className={`danger solid ${size ?? ""}`} disabled={!reason.trim() || busy}>
        {t(label)}
      </button>
      <button type="button" className={`ghost ${size ?? ""}`} onClick={() => setOpen(false)}>
        {t("Cancel")}
      </button>
    </form>
  );
}
