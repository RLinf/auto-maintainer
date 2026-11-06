// The audit log: every decision, execution, approval, pause, and configuration change,
// searchable, filterable, sortable, and groupable.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type AuditEntry } from "../api";
import { ErrorBox, PageHead, prPath, timeOf, When } from "../components";
import { useT } from "../i18n";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton } from "../ui";

export default function AuditPage() {
  const t = useT();
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api.get<AuditEntry[]>("/api/audit") });
  const list = useList<AuditEntry>({
    rows: audit.data ?? [],
    key: (a) => a.id,
    search: (a) => `${a.kind} ${a.actor} ${a.repo ?? ""}#${a.number ?? ""} ${a.summary}`,
    facets: [
      { id: "kind", label: t("Kind"), values: (a) => a.kind },
      { id: "actor", label: t("Actor"), values: (a) => a.actor },
      { id: "repo", label: t("Repository"), values: (a) => a.repo ?? t("service") },
    ],
    sorts: [
      { id: "when", label: t("Time"), value: (a) => timeOf(a.created_at), descFirst: true },
      { id: "kind", label: t("Kind"), value: (a) => a.kind },
      { id: "actor", label: t("Actor"), value: (a) => a.actor },
    ],
    defaultSort: "-when",
    groups: [
      { id: "kind", label: t("Kind"), value: (a) => a.kind },
      { id: "actor", label: t("Actor"), value: (a) => a.actor },
      { id: "repo", label: t("Repository"), value: (a) => a.repo ?? t("service") },
      { id: "day", label: t("Day"), value: (a) => a.created_at.slice(0, 10) },
    ],
    pageSize: 100,
  });
  const columns: Column<AuditEntry>[] = [
    { id: "when", header: t("When"), sort: "when", cell: (a) => <When at={a.created_at} />, width: 100 },
    { id: "kind", header: t("Kind"), sort: "kind", cell: (a) => <span className="mono">{a.kind}</span> },
    { id: "actor", header: t("Actor"), sort: "actor", cell: (a) => a.actor },
    {
      id: "target",
      header: t("Target"),
      className: "small",
      cell: (a) =>
        a.repo && a.number != null ? <Link to={prPath(a.repo, a.number)}>{`${a.repo}#${a.number}`}</Link> : a.repo ? <Link to={`/repos/${a.repo}`}>{a.repo}</Link> : <span className="muted">{t("service")}</span>,
    },
    { id: "summary", header: t("Summary"), cell: (a) => <span className="small">{a.summary}</span> },
  ];
  return (
    <>
      <PageHead title={t("Audit log")} sub={t("Everything the service and its users decided or changed.")} />
      <ListToolbar list={list} placeholder={t("Search the audit log")} />
      <ErrorBox error={audit.error} />
      {audit.isLoading ? (
        <div className="card">
          <Skeleton rows={8} />
        </div>
      ) : (
        <DataTable list={list} columns={columns} empty={t("Nothing recorded yet.")} />
      )}
    </>
  );
}
