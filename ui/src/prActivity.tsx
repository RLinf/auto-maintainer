// PR open and merge count folded-line chart on each repository's Overview tab.

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

const PERIODS = [14, 30, 90] as const;
type Mode = "daily" | "backlog";

function niceMax(raw: number): number {
  if (raw <= 4) return 4;
  if (raw <= 10) return Math.ceil(raw / 2) * 2;
  const mag = 10 ** Math.floor(Math.log10(raw));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = Math.ceil(raw / (step * mag / 5)) * (step * mag / 5);
    if (candidate >= raw && candidate / 4 === Math.floor(candidate / 4)) return candidate;
  }
  return Math.ceil(raw / mag) * mag;
}

function shortDate(iso: string): string {
  return iso.slice(5);
}

export function PrActivitySection({ repo }: { repo: string }) {
  const t = useT();
  const [days, setDays] = useState<(typeof PERIODS)[number]>(30);
  const [mode, setMode] = useState<Mode>("daily");
  const [hover, setHover] = useState<number | null>(null);

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
          <FoldedLineChart series={series} mode={mode} hover={hover} onHover={setHover} />
        </div>
      )}
    </Section>
  );
}

function FoldedLineChart({
  series,
  mode,
  hover,
  onHover,
}: {
  series: PrActivityDay[];
  mode: Mode;
  hover: number | null;
  onHover: (idx: number | null) => void;
}) {
  const t = useT();
  const W = 760;
  const H = 200;
  const padL = 36;
  const padR = 14;
  const padT = 14;
  const padB = 26;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = series.length;

  const rawMax =
    mode === "daily"
      ? Math.max(0, ...series.map((d) => Math.max(d.opened, d.merged)))
      : Math.max(0, ...series.map((d) => d.open_count));
  const yMax = niceMax(rawMax);
  const yTicks = [0, Math.round(yMax / 2), yMax];

  const xOf = (i: number) => (n <= 1 ? padL + plotW / 2 : padL + (i / (n - 1)) * plotW);
  const yOf = (v: number) => padT + plotH - (v / yMax) * plotH;

  const linePoints = (pick: (d: PrActivityDay) => number) => series.map((d, i) => `${xOf(i).toFixed(1)},${yOf(pick(d)).toFixed(1)}`).join(" ");
  const areaPath = (pick: (d: PrActivityDay) => number) => {
    if (!n) return "";
    const pts = series.map((d, i) => `${xOf(i).toFixed(1)},${yOf(pick(d)).toFixed(1)}`);
    const baseY = (padT + plotH).toFixed(1);
    return `M ${xOf(0).toFixed(1)},${baseY} L ${pts.join(" L ")} L ${xOf(n - 1).toFixed(1)},${baseY} Z`;
  };

  // Pick 5 evenly spaced X labels (start, 3 middle, end).
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
  const tipPct = hover !== null && n > 1 ? ((xOf(hover) / W) * 100) : 50;
  const clampedTipPct = Math.max(14, Math.min(86, tipPct));

  return (
    <div className="pr-curve" onMouseLeave={() => onHover(null)}>
      {active && (
        <span className="chart-tip" style={{ left: `${clampedTipPct}%` }} role="tooltip">
          <strong>{active.day}</strong> · {t("Opened")} {active.opened} · {t("Merged")} {active.merged}
          {active.closed > 0 ? ` · ${t("Closed")} ${active.closed}` : ""} · {t("Open PRs")} {active.open_count}
        </span>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} className="pr-curve-svg" role="img" aria-label={t("PR activity")}>
        {yTicks.map((tick) => {
          const y = yOf(tick);
          return (
            <g key={tick}>
              <line x1={padL} y1={y} x2={W - padR} y2={y} className="pr-curve-grid" strokeDasharray={tick === 0 ? undefined : "3 3"} />
              <text x={padL - 8} y={y + 4} textAnchor="end" className="pr-curve-axis">
                {tick}
              </text>
            </g>
          );
        })}

        {mode === "daily" ? (
          <>
            <path d={areaPath((d) => d.opened)} fill="var(--accent)" fillOpacity="0.08" />
            <path d={areaPath((d) => d.merged)} fill="var(--ok)" fillOpacity="0.08" />
            <polyline points={linePoints((d) => d.opened)} fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" />
            <polyline points={linePoints((d) => d.merged)} fill="none" stroke="var(--ok)" strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" />
          </>
        ) : (
          <>
            <path d={areaPath((d) => d.open_count)} fill="var(--accent)" fillOpacity="0.12" />
            <polyline points={linePoints((d) => d.open_count)} fill="none" stroke="var(--accent)" strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" />
          </>
        )}

        {hover !== null && active && (
          <line x1={xOf(hover)} y1={padT} x2={xOf(hover)} y2={padT + plotH} className="pr-curve-cross" />
        )}

        {n <= 45 &&
          series.map((d, i) =>
            mode === "daily" ? (
              <g key={d.day}>
                <circle cx={xOf(i)} cy={yOf(d.opened)} r={hover === i ? 4.5 : 2.3} fill="var(--accent)" stroke="var(--surface)" strokeWidth={hover === i ? 1.5 : 0.8} />
                <circle cx={xOf(i)} cy={yOf(d.merged)} r={hover === i ? 4.5 : 2.3} fill="var(--ok)" stroke="var(--surface)" strokeWidth={hover === i ? 1.5 : 0.8} />
              </g>
            ) : (
              <circle key={d.day} cx={xOf(i)} cy={yOf(d.open_count)} r={hover === i ? 4.5 : 2.3} fill="var(--accent)" stroke="var(--surface)" strokeWidth={hover === i ? 1.5 : 0.8} />
            ),
          )}

        {n > 45 && hover !== null && active && (
          mode === "daily" ? (
            <g>
              <circle cx={xOf(hover)} cy={yOf(active.opened)} r={4.5} fill="var(--accent)" stroke="var(--surface)" strokeWidth={1.5} />
              <circle cx={xOf(hover)} cy={yOf(active.merged)} r={4.5} fill="var(--ok)" stroke="var(--surface)" strokeWidth={1.5} />
            </g>
          ) : (
            <circle cx={xOf(hover)} cy={yOf(active.open_count)} r={4.5} fill="var(--accent)" stroke="var(--surface)" strokeWidth={1.5} />
          )
        )}

        {xIndices.map((idx) => (
          <text key={series[idx].day} x={xOf(idx)} y={H - 6} textAnchor={idx === 0 ? "start" : idx === n - 1 ? "end" : "middle"} className="pr-curve-axis">
            {shortDate(series[idx].day)}
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
              onMouseEnter={() => onHover(i)}
              onFocus={() => onHover(i)}
              onBlur={() => onHover(null)}
              tabIndex={0}
              aria-label={`${d.day}: ${t("Opened")} ${d.opened}, ${t("Merged")} ${d.merged}, ${t("Open PRs")} ${d.open_count}`}
            />
          );
        })}
      </svg>
    </div>
  );
}
