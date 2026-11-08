// CI health: diagnosed failures over 14 days, by job and category, with every observation
// searchable and sortable.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type CiObservation } from "../api";
import { useAgent } from "../agent";
import { ErrorBox, PageHead, prPath, Status, timeOf, When } from "../components";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton, Tabs, useTab } from "../ui";

interface JobRow {
  job: string;
  flaky: number;
  infra: number;
  pr_caused: number;
  unknown: number;
  total: number;
}

const CATEGORY: Record<string, string> = { flaky: "Flaky", infra: "Infrastructure", pr_caused: "Caused by the PR", unknown: "Unknown" };

export default function CiPage() {
  const t = useT();
  const agent = useAgent();
  const ci = useQuery({
    queryKey: ["ci"],
    queryFn: () => api.get<{ observations: CiObservation[]; by_job: Record<string, Record<string, number>> }>("/api/ci"),
  });
  const obs = ci.data?.observations ?? [];
  const jobs: JobRow[] = Object.entries(ci.data?.by_job ?? {}).map(([job, c]) => {
    const row = { job, flaky: c.flaky ?? 0, infra: c.infra ?? 0, pr_caused: c.pr_caused ?? 0, unknown: c.unknown ?? 0 };
    return { ...row, total: row.flaky + row.infra + row.pr_caused + row.unknown };
  });
  const tabs = [
    { id: "jobs", label: t("By job"), count: jobs.length },
    { id: "all", label: t("Every failure"), count: obs.length },
  ];
  const [tab, setTab] = useTab(tabs);

  const jobList = useList<JobRow>({
    rows: jobs,
    key: (j) => j.job,
    search: (j) => j.job,
    sorts: [
      { id: "total", label: t("Failures"), value: (j) => j.total, descFirst: true },
      { id: "flaky", label: t("Flaky"), value: (j) => j.flaky, descFirst: true },
      { id: "infra", label: t("Infrastructure"), value: (j) => j.infra, descFirst: true },
      { id: "pr", label: t("Caused by the PR"), value: (j) => j.pr_caused, descFirst: true },
      { id: "job", label: t("Job"), value: (j) => j.job },
    ],
    defaultSort: "-flaky",
    param: "j",
  });
  const obsList = useList<CiObservation>({
    rows: obs,
    key: (o) => `${o.check_id}:${o.created_at}`,
    search: (o) => `${o.repo}#${o.number ?? ""} ${o.workflow ?? ""} ${o.check_name} ${o.signature ?? ""} ${o.explanation}`,
    facets: [
      { id: "repo", label: t("Repository"), values: (o) => o.repo },
      { id: "category", label: t("Category"), values: (o) => t(CATEGORY[o.category] ?? o.category) },
      { id: "job", label: t("Job"), values: (o) => o.check_name },
      { id: "rerun", label: t("Rerun"), values: (o) => (o.rerun ? t("Rerun") : t("Not rerun")) },
    ],
    sorts: [
      { id: "when", label: t("Time"), value: (o) => timeOf(o.created_at), descFirst: true },
      { id: "job", label: t("Job"), value: (o) => o.check_name },
      { id: "confidence", label: t("Confidence"), value: (o) => o.confidence, descFirst: true },
    ],
    defaultSort: "-when",
    groups: [
      { id: "category", label: t("Category"), value: (o) => t(CATEGORY[o.category] ?? o.category) },
      { id: "job", label: t("Job"), value: (o) => o.check_name },
      { id: "repo", label: t("Repository"), value: (o) => o.repo },
    ],
    param: "o",
  });
  const sum = (k: keyof JobRow) => jobs.reduce((n, j) => n + (j[k] as number), 0);
  const jobColumns: Column<JobRow>[] = [
    { id: "job", primary: true, header: t("Job"), sort: "job", cell: (j) => <span className="mono">{j.job}</span> },
    { id: "flaky", header: t("Flaky"), sort: "flaky", className: "num", cell: (j) => j.flaky || <span className="muted">0</span> },
    { id: "infra", header: t("Infrastructure"), sort: "infra", className: "num", cell: (j) => j.infra || <span className="muted">0</span> },
    { id: "pr", header: t("Caused by the PR"), sort: "pr", className: "num", cell: (j) => j.pr_caused || <span className="muted">0</span> },
    { id: "unknown", header: t("Unknown"), className: "num", cell: (j) => j.unknown || <span className="muted">0</span> },
    { id: "total", header: t("Total"), sort: "total", className: "num", cell: (j) => <strong>{j.total}</strong> },
  ];
  const obsColumns: Column<CiObservation>[] = [
    { id: "when", header: t("When"), sort: "when", cell: (o) => <When at={o.created_at} />, width: 96 },
    {
      id: "pr",
      primary: true,
      header: t("Pull request"),
      cell: (o) => (o.number != null ? <Link to={prPath(o.repo, o.number)}>{`${o.repo}#${o.number}`}</Link> : o.repo),
      className: "small",
    },
    { id: "job", header: t("Job"), sort: "job", cell: (o) => <span className="small">{o.workflow ? `${o.workflow} / ${o.check_name}` : o.check_name}</span> },
    {
      id: "category",
      header: t("Category"),
      cell: (o) => (
        <span className="row" style={{ gap: 6 }}>
          <Status value={o.category} label={t(CATEGORY[o.category] ?? o.category)} />
          {o.rerun && (
            <span className="small muted">
              <Icon name="refresh" size={12} /> {t("rerun")}
            </span>
          )}
        </span>
      ),
    },
    { id: "sig", header: t("Signature"), cell: (o) => (o.signature ? <code>{o.signature}</code> : <span className="muted">—</span>), className: "hide-narrow" },
    { id: "why", header: t("Explanation"), cell: (o) => <span className="small">{o.explanation}</span> },
  ];

  return (
    <>
      <PageHead
        title={t("CI health")}
        sub={t("Failed jobs the service diagnosed in the last 14 days. Flaky and infrastructure failures can be rerun automatically.")}
        actions={
          <button className="agent" onClick={() => agent.open({ prompt: t("Which CI jobs are flaky, and should anything change?"), send: false })}>
            <Icon name="sparkles" />
            {t("Ask about CI")}
          </button>
        }
      />
      <ErrorBox error={ci.error} />
      {ci.isLoading ? (
        <div className="card">
          <Skeleton rows={6} />
        </div>
      ) : (
        <>
          <div className="tiles">
            {(
              [
                ["Failures diagnosed", sum("total"), "activity"],
                ["Flaky", sum("flaky"), "refresh"],
                ["Infrastructure", sum("infra"), "server"],
                ["Caused by the PR", sum("pr_caused"), "pr"],
              ] as const
            ).map(([label, value, icon]) => (
              <div key={label} className="tile">
                <span className="tile-label">
                  <Icon name={icon} size={15} />
                  {t(label)}
                </span>
                <span className="tile-value">{value}</span>
              </div>
            ))}
          </div>
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
          {tab === "jobs" ? (
            <>
              <ListToolbar list={jobList} placeholder={t("Search jobs")} />
              <DataTable list={jobList} columns={jobColumns} empty={t("No failed jobs diagnosed.")} />
            </>
          ) : (
            <>
              <ListToolbar list={obsList} placeholder={t("Search failures")} />
              <DataTable list={obsList} columns={obsColumns} empty={t("No failures diagnosed.")} />
            </>
          )}
        </>
      )}
    </>
  );
}
