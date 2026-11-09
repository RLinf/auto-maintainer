// Agent runs: what the agents did, how long it took and what it cost, per runtime and
// task. Choosing agents and models happens in Settings.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type AgentHealth, type AgentRun } from "../api";
import { ErrorBox, PageHead, Status, timeOf, When } from "../components";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton } from "../ui";
import { AgentsUsageSection } from "../usage";

export function useAgentsData() {
  return useQuery({ queryKey: ["agents"], queryFn: () => api.get<{ health: AgentHealth[]; runs: AgentRun[] }>("/api/agents") });
}

export default function AgentsPage() {
  const t = useT();
  const agents = useAgentsData();
  const runs = agents.data?.runs ?? [];
  const list = useList<AgentRun>({
    rows: runs,
    key: (r) => r.id,
    search: (r) => `${r.runtime} ${r.task_kind} ${r.repo ?? ""} ${r.error ?? ""}`,
    facets: [
      { id: "runtime", label: t("Agent"), values: (r) => r.runtime },
      { id: "task", label: t("Task"), values: (r) => r.task_kind },
      { id: "result", label: t("Result"), values: (r) => (r.ok ? t("Succeeded") : t("Failed")) },
      { id: "repo", label: t("Repository"), values: (r) => r.repo ?? t("service") },
    ],
    sorts: [
      { id: "when", label: t("Time"), value: (r) => timeOf(r.created_at), descFirst: true },
      { id: "cost", label: t("Cost"), value: (r) => r.cost_usd, descFirst: true },
      { id: "duration", label: t("Duration"), value: (r) => r.duration_seconds, descFirst: true },
      { id: "tokens", label: t("Tokens"), value: (r) => r.input_tokens + r.output_tokens, descFirst: true },
    ],
    defaultSort: "-when",
    groups: [
      { id: "runtime", label: t("Agent"), value: (r) => r.runtime },
      { id: "task", label: t("Task"), value: (r) => r.task_kind },
    ],
  });
  const shown = list.all;
  const cost = shown.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0);
  const ok = shown.filter((r) => r.ok).length;
  const durations = shown.map((r) => r.duration_seconds).sort((a, b) => a - b);
  const median = durations.length ? durations[Math.floor(durations.length / 2)] : 0;
  const columns: Column<AgentRun>[] = [
    { id: "when", header: t("When"), sort: "when", cell: (r) => <When at={r.created_at} />, width: 96 },
    { id: "runtime", header: t("Agent"), cell: (r) => <span className="mono">{r.runtime}</span> },
    {
      id: "task",
      primary: true,
      header: t("Task"),
      cell: (r) => (
        <span className="row" style={{ gap: 6 }}>
          {r.task_kind}
          {r.permissions === "bypass" && (
            <span className="pill status-warn" title={t("Ran with permission checks skipped")}>
              bypass
            </span>
          )}
        </span>
      ),
    },
    { id: "repo", header: t("Repository"), cell: (r) => r.repo ?? <span className="muted">—</span>, className: "small hide-narrow" },
    { id: "for", header: t("For"), cell: (r) => r.requested_by ?? <span className="muted">{t("the service")}</span>, className: "small hide-narrow" },
    {
      id: "result",
      header: t("Result"),
      cell: (r) => (
        <span title={r.error ?? ""}>
          <Status value={r.ok ? "executed" : "failed"} label={r.ok ? t("ok") : t("failed")} /> {r.error && <span className="small muted truncate">{r.error.slice(0, 80)}</span>}
        </span>
      ),
    },
    { id: "tokens", header: t("Tokens"), sort: "tokens", className: "num hide-narrow", cell: (r) => (r.input_tokens + r.output_tokens).toLocaleString() },
    { id: "cost", header: t("Cost"), sort: "cost", className: "num", cell: (r) => (r.cost_usd == null ? <span className="muted">—</span> : `$${r.cost_usd.toFixed(3)}`) },
    { id: "duration", header: t("Seconds"), sort: "duration", className: "num", cell: (r) => r.duration_seconds.toFixed(1) },
  ];

  return (
    <>
      <PageHead
        title={t("Agents")}
        sub={t("Every agent run: which CLI, which task, how it went, and what it cost.")}
        actions={
          <Link to="/settings?tab=agents" className="button">
            <Icon name="cpu" />
            {t("Choose agents and models")}
          </Link>
        }
      />
      <ErrorBox error={agents.error} />
      <AgentsUsageSection />
      {agents.isLoading ? (
        <div className="card">
          <Skeleton rows={6} />
        </div>
      ) : (
        <>
          <div className="tiles">
            <div className="tile">
              <span className="tile-label">
                <Icon name="activity" size={15} />
                {t("Runs")}
              </span>
              <span className="tile-value">{shown.length}</span>
              <span className="tile-foot">{list.filtering ? t("matching the filters") : t("recent runs")}</span>
            </div>
            <div className="tile">
              <span className="tile-label">
                <Icon name="check" size={15} />
                {t("Succeeded")}
              </span>
              <span className="tile-value">{shown.length ? `${Math.round((ok / shown.length) * 100)}%` : "—"}</span>
              <span className="tile-foot">
                {ok} / {shown.length}
              </span>
            </div>
            <div className="tile">
              <span className="tile-label">
                <Icon name="clock" size={15} />
                {t("Median duration")}
              </span>
              <span className="tile-value">{median ? `${median.toFixed(0)}s` : "—"}</span>
            </div>
            <div className="tile">
              <span className="tile-label">
                <Icon name="server" size={15} />
                {t("Reported cost")}
              </span>
              <span className="tile-value">${cost.toFixed(2)}</span>
            </div>
          </div>
          <ListToolbar list={list} placeholder={t("Search runs")} />
          <DataTable list={list} columns={columns} empty={t("No agent runs yet.")} />
        </>
      )}
    </>
  );
}
