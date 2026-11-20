// Repository Statistics tab: Star history, PR open/merge/backlog, and Issue
// open/close/backlog folded-line charts.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { Empty, ErrorBox, Section } from "./components";
import { useT } from "./i18n";
import { Skeleton } from "./ui";

export interface PrActivityDay {
  day: string;
  opened: number;
  merged: number;
  closed: number;
  open_count: number;
}

export interface PrActivitySeries {
  repo: string;
  days: number;
  series: PrActivityDay[];
  total_opened: number;
  total_merged: number;
  total_closed: number;
  current_open: number;
}

export interface IssueActivityDay {
  day: string;
  opened: number;
  closed: number;
  open_count: number;
}

export interface IssueActivitySeries {
  repo: string;
  days: number;
  series: IssueActivityDay[];
  total_opened: number;
  total_closed: number;
  current_open: number;
}

export interface StarDay {
  day: string;
  added: number;
  total: number;
}

export interface StarHistorySeries {
  repo: string;
  days: number | null;
  total: number;
  added_in_period: number;
  sampled: boolean;
  series: StarDay[];
}

const PERIODS = [14, 30, 90] as const;
const STAR_PERIODS: { id: "all" | 90 | 30 | 14; label: string }[] = [
  { id: "all", label: "All time" },
  { id: 90, label: "90d" },
  { id: 30, label: "30d" },
  { id: 14, label: "14d" },
];

function niceMax(raw: number): number {
  if (raw <= 4) return 4;
  if (raw <= 10) return Math.ceil(raw / 2) * 2;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = mag / 2;
  return Math.ceil(raw / step) * step;
}

function fmtTick(n: number): string {
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

function fmtAxisDate(iso: string, multiYear: boolean): string {
  return multiYear ? iso.slice(0, 7) : iso.slice(5);
}

interface LineSpec<T> {
  key: string;
  color: string;
  value: (d: T) => number;
  fillOpacity?: number;
}

function FoldedLineSvg<T extends { day: string }>({
  series,
  lines,
  label,
  tip,
  yMinZero = true,
}: {
  series: T[];
  lines: LineSpec<T>[];
  label: string;
  tip: (d: T) => string;
  yMinZero?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 760;
  const H = 200;
  const padL = 44;
  const padR = 14;
  const padT = 14;
  const padB = 26;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = series.length;

  const allVals = series.flatMap((d) => lines.map((l) => l.value(d)));
  const rawMax = Math.max(0, ...allVals);
  const rawMin = allVals.length ? Math.min(...allVals) : 0;
  const yMin = yMinZero ? 0 : Math.max(0, Math.floor(rawMin - Math.max(1, (rawMax - rawMin) * 0.15)));
  const yMax = yMin === 0 ? niceMax(rawMax) : Math.max(yMin + 4, Math.ceil(rawMax + Math.max(1, (rawMax - rawMin) * 0.1)));
  const ySpan = Math.max(1, yMax - yMin);
  const yTicks = [yMin, Math.round(yMin + ySpan / 2), yMax];

  const xOf = (i: number) => (n <= 1 ? padL + plotW / 2 : padL + (i / (n - 1)) * plotW);
  const yOf = (v: number) => padT + plotH - ((v - yMin) / ySpan) * plotH;

  const linePoints = (pick: (d: T) => number) => series.map((d, i) => `${xOf(i).toFixed(1)},${yOf(pick(d)).toFixed(1)}`).join(" ");
  const areaPath = (pick: (d: T) => number) => {
    if (!n) return "";
    const pts = series.map((d, i) => `${xOf(i).toFixed(1)},${yOf(pick(d)).toFixed(1)}`);
    const baseY = (padT + plotH).toFixed(1);
    return `M ${xOf(0).toFixed(1)},${baseY} L ${pts.join(" L ")} L ${xOf(n - 1).toFixed(1)},${baseY} Z`;
  };

  const multiYear = n > 1 && series[0].day.slice(0, 4) !== series[n - 1].day.slice(0, 4);
  const labelCount = Math.min(n, 6);
  const xIndices = Array.from(
    new Set(
      Array.from({ length: labelCount }, (_, k) =>
        labelCount <= 1 ? 0 : Math.round((k * (n - 1)) / (labelCount - 1)),
      ),
    ),
  );

  const active = hover !== null && hover >= 0 && hover < n ? series[hover] : null;
  const colW = n > 1 ? plotW / (n - 1) : plotW;
  const tipPct = hover !== null && n > 1 ? (xOf(hover) / W) * 100 : 50;
  const clampedTipPct = Math.max(14, Math.min(86, tipPct));

  return (
    <div className="pr-curve" onMouseLeave={() => setHover(null)}>
      {active && (
        <span className="chart-tip" style={{ left: `${clampedTipPct}%` }} role="tooltip">
          {tip(active)}
        </span>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} className="pr-curve-svg" role="img" aria-label={label}>
        {yTicks.map((tick, idx) => {
          const y = yOf(tick);
          return (
            <g key={`${tick}-${idx}`}>
              <line x1={padL} y1={y} x2={W - padR} y2={y} className="pr-curve-grid" strokeDasharray={idx === 0 ? undefined : "3 3"} />
              <text x={padL - 8} y={y + 4} textAnchor="end" className="pr-curve-axis">
                {fmtTick(tick)}
              </text>
            </g>
          );
        })}

        {lines.map((l) => (
          <g key={l.key}>
            <path d={areaPath(l.value)} fill={l.color} fillOpacity={l.fillOpacity ?? 0.08} />
            <polyline points={linePoints(l.value)} fill="none" stroke={l.color} strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}

        {hover !== null && active && (
          <line x1={xOf(hover)} y1={padT} x2={xOf(hover)} y2={padT + plotH} className="pr-curve-cross" />
        )}

        {n <= 45 &&
          series.map((d, i) => (
            <g key={d.day}>
              {lines.map((l) => (
                <circle
                  key={l.key}
                  cx={xOf(i)}
                  cy={yOf(l.value(d))}
                  r={hover === i ? 4.5 : 2.3}
                  fill={l.color}
                  stroke="var(--surface)"
                  strokeWidth={hover === i ? 1.5 : 0.8}
                />
              ))}
            </g>
          ))}

        {n > 45 && hover !== null && active && (
          <g>
            {lines.map((l) => (
              <circle
                key={l.key}
                cx={xOf(hover)}
                cy={yOf(l.value(active))}
                r={4.5}
                fill={l.color}
                stroke="var(--surface)"
                strokeWidth={1.5}
              />
            ))}
          </g>
        )}

        {xIndices.map((idx) => (
          <text
            key={series[idx].day}
            x={xOf(idx)}
            y={H - 6}
            textAnchor={idx === 0 ? "start" : idx === n - 1 ? "end" : "middle"}
            className="pr-curve-axis"
          >
            {fmtAxisDate(series[idx].day, multiYear)}
          </text>
        ))}

        {series.map((d, i) => {
          const x = Math.max(padL, xOf(i) - colW / 2);
          const right = Math.min(W - padR, xOf(i) + colW / 2);
          return (
            <rect
              key={d.day}
              x={x}
              y={padT}
              width={Math.max(4, right - x)}
              height={plotH}
              fill="transparent"
              style={{ cursor: "crosshair" }}
              onMouseEnter={() => setHover(i)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              tabIndex={0}
              aria-label={tip(d)}
            />
          );
        })}
      </svg>
    </div>
  );
}

export function StarHistorySection({ repo }: { repo: string }) {
  const t = useT();
  const [period, setPeriod] = useState<"all" | 90 | 30 | 14>("all");
  const [mode, setMode] = useState<"total" | "daily">("total");

  const q = useQuery({
    queryKey: ["star-history", repo, period],
    queryFn: () =>
      api.get<StarHistorySeries>(
        period === "all" ? `/api/repos/${repo}/star-history` : `/api/repos/${repo}/star-history?days=${period}`,
      ),
  });

  const data = q.data;
  const series = data?.series ?? [];
  const showDaily = period !== "all" && mode === "daily";

  return (
    <Section
      title={t("Star history")}
      hint={
        period === "all"
          ? t("Cumulative stars over time (UTC)")
          : showDaily
            ? t("New stars per day (UTC)")
            : t("Cumulative stars over the period (UTC)")
      }
      aside={
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          {period !== "all" && (
            <div className="segmented" role="group" aria-label={t("Chart view")}>
              <button className={mode === "total" ? "on" : ""} onClick={() => setMode("total")}>
                {t("Cumulative")}
              </button>
              <button className={mode === "daily" ? "on" : ""} onClick={() => setMode("daily")}>
                {t("Daily new")}
              </button>
            </div>
          )}
          <div className="segmented" role="group" aria-label={t("Period")}>
            {STAR_PERIODS.map((p) => (
              <button key={String(p.id)} className={period === p.id ? "on" : ""} onClick={() => setPeriod(p.id)}>
                {t(p.label)}
              </button>
            ))}
          </div>
        </div>
      }
    >
      <ErrorBox error={q.error} />
      {q.isLoading ? (
        <Skeleton rows={4} />
      ) : !data || !series.length ? (
        <Empty icon="sparkles">{t("No star history yet.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "center" }}>
            <span className="chart-legend">
              <span className="chart-key">
                <i style={{ ["--c" as string]: "var(--warn)" }} />
                {t("Total stars")}: <strong>{data.total.toLocaleString()}</strong>
              </span>
            </span>
            {period !== "all" && (
              <span className="small muted">
                {t("New in")} {period}d: <strong style={{ color: "var(--ok)" }}>+{data.added_in_period.toLocaleString()}</strong>
              </span>
            )}
            {period === "all" && data.sampled && <span className="small muted">{t("sampled across all pages")}</span>}
          </div>
          <FoldedLineSvg
            series={series}
            label={t("Star history")}
            yMinZero={period === "all" || showDaily}
            lines={[
              {
                key: showDaily ? "added" : "total",
                color: "var(--warn)",
                value: (d) => (showDaily ? d.added : d.total),
                fillOpacity: 0.14,
              },
            ]}
            tip={(d) =>
              period === "all"
                ? `${d.day} · ${t("Total stars")} ${d.total.toLocaleString()}`
                : `${d.day} · +${d.added} · ${t("Total stars")} ${d.total.toLocaleString()}`
            }
          />
        </div>
      )}
    </Section>
  );
}

export function PrActivitySection({ repo }: { repo: string }) {
  const t = useT();
  const [days, setDays] = useState<(typeof PERIODS)[number]>(30);
  const [mode, setMode] = useState<"daily" | "backlog">("daily");

  const q = useQuery({
    queryKey: ["pr-activity", repo, days],
    queryFn: () => api.get<PrActivitySeries>(`/api/repos/${repo}/pr-activity?days=${days}`),
  });

  const data = q.data;
  const series = data?.series ?? [];
  const net = data ? data.total_opened - data.total_merged : 0;

  return (
    <Section
      title={t("PR activity")}
      hint={mode === "daily" ? t("PRs opened and merged per day (UTC)") : t("Open PR count at the end of each day (UTC)")}
      aside={
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <div className="segmented" role="group" aria-label={t("Chart view")}>
            <button className={mode === "daily" ? "on" : ""} onClick={() => setMode("daily")}>
              {t("Opened & merged")}
            </button>
            <button className={mode === "backlog" ? "on" : ""} onClick={() => setMode("backlog")}>
              {t("Open backlog")}
            </button>
          </div>
          <div className="segmented" role="group" aria-label={t("Period")}>
            {PERIODS.map((p) => (
              <button key={p} className={days === p ? "on" : ""} onClick={() => setDays(p)}>
                {p}d
              </button>
            ))}
          </div>
        </div>
      }
    >
      <ErrorBox error={q.error} />
      {q.isLoading ? (
        <Skeleton rows={4} />
      ) : !data || !series.length ? (
        <Empty icon="pr">{t("No PR activity yet.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "center" }}>
            <span className="chart-legend">
              {mode === "daily" ? (
                <>
                  <span className="chart-key">
                    <i style={{ ["--c" as string]: "var(--accent)" }} />
                    {t("Opened")}: <strong>{data.total_opened}</strong>
                  </span>
                  <span className="chart-key">
                    <i style={{ ["--c" as string]: "var(--ok)" }} />
                    {t("Merged")}: <strong>{data.total_merged}</strong>
                  </span>
                </>
              ) : (
                <span className="chart-key">
                  <i style={{ ["--c" as string]: "var(--accent)" }} />
                  {t("Open PRs")}: <strong>{data.current_open}</strong>
                </span>
              )}
            </span>
            <span className="small muted">
              {t("Net")}: <strong style={{ color: net > 0 ? "var(--warn)" : "var(--ok)" }}>{net > 0 ? `+${net}` : net}</strong> · {t("Open now")}: <strong>{data.current_open}</strong>
            </span>
          </div>
          <FoldedLineSvg
            series={series}
            label={t("PR activity")}
            lines={
              mode === "daily"
                ? [
                    { key: "opened", color: "var(--accent)", value: (d) => d.opened },
                    { key: "merged", color: "var(--ok)", value: (d) => d.merged },
                  ]
                : [{ key: "open", color: "var(--accent)", value: (d) => d.open_count, fillOpacity: 0.12 }]
            }
            tip={(d) =>
              `${d.day} · ${t("Opened")} ${d.opened} · ${t("Merged")} ${d.merged}${d.closed > 0 ? ` · ${t("Closed")} ${d.closed}` : ""} · ${t("Open PRs")} ${d.open_count}`
            }
          />
        </div>
      )}
    </Section>
  );
}

export function IssueActivitySection({ repo }: { repo: string }) {
  const t = useT();
  const [days, setDays] = useState<(typeof PERIODS)[number]>(30);
  const [mode, setMode] = useState<"daily" | "backlog">("daily");

  const q = useQuery({
    queryKey: ["issue-activity", repo, days],
    queryFn: () => api.get<IssueActivitySeries>(`/api/repos/${repo}/issue-activity?days=${days}`),
  });

  const data = q.data;
  const series = data?.series ?? [];
  const net = data ? data.total_opened - data.total_closed : 0;

  return (
    <Section
      title={t("Issue activity")}
      hint={mode === "daily" ? t("Issues opened and closed per day (UTC)") : t("Open issue count at the end of each day (UTC)")}
      aside={
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <div className="segmented" role="group" aria-label={t("Chart view")}>
            <button className={mode === "daily" ? "on" : ""} onClick={() => setMode("daily")}>
              {t("Opened & closed")}
            </button>
            <button className={mode === "backlog" ? "on" : ""} onClick={() => setMode("backlog")}>
              {t("Open issues")}
            </button>
          </div>
          <div className="segmented" role="group" aria-label={t("Period")}>
            {PERIODS.map((p) => (
              <button key={p} className={days === p ? "on" : ""} onClick={() => setDays(p)}>
                {p}d
              </button>
            ))}
          </div>
        </div>
      }
    >
      <ErrorBox error={q.error} />
      {q.isLoading ? (
        <Skeleton rows={4} />
      ) : !data || !series.length ? (
        <Empty icon="list">{t("No issue activity yet.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "center" }}>
            <span className="chart-legend">
              {mode === "daily" ? (
                <>
                  <span className="chart-key">
                    <i style={{ ["--c" as string]: "var(--accent)" }} />
                    {t("Opened")}: <strong>{data.total_opened}</strong>
                  </span>
                  <span className="chart-key">
                    <i style={{ ["--c" as string]: "var(--ok)" }} />
                    {t("Closed")}: <strong>{data.total_closed}</strong>
                  </span>
                </>
              ) : (
                <span className="chart-key">
                  <i style={{ ["--c" as string]: "var(--accent)" }} />
                  {t("Open issues")}: <strong>{data.current_open}</strong>
                </span>
              )}
            </span>
            <span className="small muted">
              {t("Net")}: <strong style={{ color: net > 0 ? "var(--warn)" : "var(--ok)" }}>{net > 0 ? `+${net}` : net}</strong> · {t("Open now")}: <strong>{data.current_open}</strong>
            </span>
          </div>
          <FoldedLineSvg
            series={series}
            label={t("Issue activity")}
            lines={
              mode === "daily"
                ? [
                    { key: "opened", color: "var(--accent)", value: (d) => d.opened },
                    { key: "closed", color: "var(--ok)", value: (d) => d.closed },
                  ]
                : [{ key: "open", color: "var(--accent)", value: (d) => d.open_count, fillOpacity: 0.12 }]
            }
            tip={(d) => `${d.day} · ${t("Opened")} ${d.opened} · ${t("Closed")} ${d.closed} · ${t("Open issues")} ${d.open_count}`}
          />
        </div>
      )}
    </Section>
  );
}

export function RepoStatsTab({ repo }: { repo: string }) {
  return (
    <div className="stack" style={{ gap: 16 }}>
      <StarHistorySection repo={repo} />
      <PrActivitySection repo={repo} />
      <IssueActivitySection repo={repo} />
    </div>
  );
}
