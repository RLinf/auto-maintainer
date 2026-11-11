// The service's own code: what it runs, what waits on its default branch, and the
// self-improvement PRs it opened. Only maintainers of its repository merge and deploy.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type ServiceView } from "../api";
import { useAgent } from "../agent";
import { Empty, ErrorBox, PageHead, Section, Status, timeOf, useAct, When } from "../components";
import { useProposals, useService } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, useList, type Column } from "../list";
import { ProposalCard, ServiceMergeBaseButton } from "../proposal";
import { Skeleton, useToast } from "../ui";

const KEYS = [["service"], ["audit"]];
type HistoryRow = NonNullable<ServiceView["history"]>[number];

export default function ServicePage() {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const view = useService();
  const proposals = useProposals();
  const merge = useAct((n: number) => api.post(`/api/service/prs/${n}/merge`), KEYS);
  const deploy = useAct((sha: string) => api.post("/api/service/deploy", { sha }), KEYS);
  const history = useList<HistoryRow>({
    rows: view.data?.history ?? [],
    key: (h) => `${h.kind}:${h.at}:${h.summary}`,
    sorts: [{ id: "at", label: t("Time"), value: (h) => timeOf(h.at), descFirst: true }],
    defaultSort: "-at",
    facets: [
      { id: "kind", label: t("Kind"), values: (h) => h.kind },
      { id: "status", label: t("Status"), values: (h) => h.status },
    ],
    param: "h",
  });

  if (view.isLoading) {
    return (
      <div className="card">
        <Skeleton rows={6} />
      </div>
    );
  }
  if (view.error) return <ErrorBox error={view.error} />;
  const v = view.data!;
  if (!v.code_repo) {
    return (
      <>
        <PageHead title={t("Service")} />
        <Empty icon="server">{t("No code repository is configured, so the service cannot update itself. Set code_repo in local.yaml.")}</Empty>
      </>
    );
  }
  const s = v.status;
  const prs = (v.prs ?? []).filter((p) => p.self_improvement);
  const drafts = (proposals.data ?? []).filter((p) => p.action?.action.kind === "code.update");
  const columns: Column<HistoryRow>[] = [
    { id: "at", header: t("When"), sort: "at", cell: (h) => <When at={h.at} />, width: 100 },
    { id: "kind", header: t("Kind"), cell: (h) => <span className="mono">{h.kind}</span> },
    { id: "status", header: t("Status"), cell: (h) => <Status value={h.status} /> },
    { id: "summary", primary: true, header: t("Summary"), cell: (h) => <span className="small">{h.summary}</span> },
    { id: "error", header: t("Error"), cell: (h) => <span className="small muted">{h.error?.slice(0, 200)}</span>, className: "hide-narrow" },
  ];

  return (
    <>
      <PageHead
        title={t("Service")}
        sub={
          <>
            {t("The service changes its own code when reviewers ask for it in a conversation, in")} <code>{v.code_repo}</code>.{" "}
            {t("Only its owner merges and deploys:")} <strong>{v.owner ?? t("any admin of it, until one takes ownership")}</strong>. {!v.can_approve && v.reason}
          </>
        }
        actions={
          <>
            <button className="agent" onClick={() => agent.open({ prompt: t("I'd like the service to be able to "), send: false })}>
              <Icon name="sparkles" />
              {t("Request an improvement")}
            </button>
            <Link to="/settings" className="button">
              {t("Settings")}
            </Link>
          </>
        }
      />
      <ErrorBox error={merge.error || deploy.error} />
      <div className="tiles">
        <div className="tile">
          <span className="tile-label">
            <Icon name="server" size={15} />
            {t("Running")}
          </span>
          <span className="tile-value" style={{ fontSize: 22 }}>
            <code style={{ fontSize: 20 }}>{s ? s.running_sha.slice(0, 10) : "—"}</code>
          </span>
          <span className="tile-foot">
            {s?.branch ?? "—"}
            {s?.dirty ? ` · ${t("local changes")}` : ""}
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">
            <Icon name="upload" size={15} />
            {t("Waiting to deploy")}
          </span>
          <span className="tile-value">{s?.pending.length ?? 0}</span>
          <span className="tile-foot">
            {t("commits on")} {v.branch}
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">
            <Icon name="merge" size={15} />
            {t("Self-improvement PRs")}
          </span>
          <span className="tile-value">{prs.length}</span>
          <span className="tile-foot">{t("open")}</span>
        </div>
      </div>

      <div className="grid2">
        <Section
          title={t("Updates")}
          aside={
            v.can_approve && s?.upstream_sha && s.pending.length > 0 ? (
              <button
                className="primary"
                disabled={deploy.isPending || s.dirty}
                onClick={() => deploy.mutate(s.upstream_sha!, { onSuccess: () => toast({ title: t("Deploy started"), body: t("The service tests the commit, then restarts on it."), icon: "upload" }) })}
              >
                <Icon name="upload" size={14} />
                {t("Deploy")} {s.upstream_sha.slice(0, 8)}
              </button>
            ) : null
          }
        >
          {!s ? (
            <span className="muted small">{t("The service does not run from a git checkout it can update.")}</span>
          ) : s.error ? (
            <div className="error">{s.error}</div>
          ) : s.pending.length === 0 ? (
            <span className="small muted row" style={{ gap: 6 }}>
              <Icon name="check" size={14} /> {t("Up to date with")} {v.branch}.
            </span>
          ) : (
            <div className="list">
              {s.pending.map((c) => (
                <div key={c.sha} className="list-row">
                  <code>{c.sha.slice(0, 8)}</code>
                  <span className="main">
                    <span className="t">{c.subject}</span>
                    <span className="meta">{c.author}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>
        <Section title={t("Self-improvement PRs")}>
          {prs.length === 0 ? (
            <span className="small muted">{t("No open self-improvement PRs.")}</span>
          ) : (
            <div className="list">
              {prs.map((p) => {
                const base = p.base ?? v.branch ?? "main";
                const conflicting = !!p.conflicting;
                return (
                  <div key={p.number} className="list-row" style={{ flexWrap: "wrap" }}>
                    <Icon name="merge" />
                    <span className="main">
                      <a className="t" href={p.url} target="_blank" rel="noreferrer">
                        #{p.number} {p.title}
                      </a>
                      <span className="meta">
                        {p.branch}
                        {conflicting ? ` · ${t("conflicts with")} ${base}` : ""}
                      </span>
                    </span>
                    {conflicting && <Status value="conflict" />}
                    {v.can_approve && (
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <ServiceMergeBaseButton number={p.number} base={base} conflicting={conflicting} size="sm" />
                        <button
                          className={conflicting ? "sm" : "sm primary"}
                          disabled={merge.isPending || conflicting}
                          title={conflicting ? `${t("conflicts with")} ${base}` : undefined}
                          onClick={() => merge.mutate(p.number, { onSuccess: () => toast({ title: `#${p.number} ${t("merged")}`, icon: "merge" }) })}
                        >
                          {t("Approve and merge")}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Section>
      </div>
      {drafts.length > 0 && (
        <Section title={t("Merge resolutions to approve")}>
          <div className="stack" style={{ gap: 10 }}>
            {drafts.map((d) => (
              <ProposalCard key={d.id} proposal={d} />
            ))}
          </div>
        </Section>
      )}
      <h2>{t("History")}</h2>
      <DataTable list={history} columns={columns} empty={t("Nothing yet.")} />
      {v.can_approve && <LogsSection />}
    </>
  );
}

/** The end of the log ``am start`` keeps, refreshed every few seconds while open. */
function LogsSection() {
  const t = useT();
  const [live, setLive] = useState(true);
  const [filter, setFilter] = useState("");
  const [problems, setProblems] = useState(false);
  const [requests, setRequests] = useState(false);
  const logs = useQuery({
    queryKey: ["service", "logs"],
    queryFn: () => api.get<{ path: string; lines: string[]; kept: boolean }>("/api/service/logs?lines=500"),
    refetchInterval: live ? 4000 : false,
  });
  const box = useRef<HTMLPreElement>(null);
  const lines = (logs.data?.lines ?? []).filter(
    (l) =>
      (!filter || l.toLowerCase().includes(filter.toLowerCase())) &&
      (!problems || /\b(ERROR|WARNING|CRITICAL|Traceback)\b/.test(l)) &&
      // the dashboard's own requests (this view polls too) are noise unless asked for
      (requests || !/"(GET|POST|PUT|DELETE|PATCH) \//.test(l)),
  );
  useEffect(() => {
    const el = box.current;
    if (el && live) el.scrollTop = el.scrollHeight;
  }, [lines.length, live]);
  return (
    <section className="stack" style={{ gap: 10, marginTop: 18 }}>
      <div className="row">
        <h2>{t("Logs")}</h2>
        {logs.data?.path && (
          <code className="small muted ellipsis" title={logs.data.path} style={{ maxWidth: 260 }}>
            {logs.data.path.split("/").pop()}
          </code>
        )}
        <span className="spacer" />
        <input placeholder={t("Filter")} value={filter} onChange={(e) => setFilter(e.target.value)} className="log-filter" />
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} />
          {t("Errors and warnings only")}
        </label>
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={requests} onChange={(e) => setRequests(e.target.checked)} />
          {t("Show HTTP requests")}
        </label>
        <button className={`sm ${live ? "on" : ""}`} onClick={() => setLive(!live)}>
          <Icon name={live ? "pause" : "play"} size={13} />
          {live ? t("Pause") : t("Follow")}
        </button>
      </div>
      {logs.data && !logs.data.kept ? (
        <Empty icon="list">
          {t("This service writes no log file: it was not started with am start, so its log goes to the terminal that started it.")}
        </Empty>
      ) : (
        <pre ref={box} className="log-view">
          {lines.length ? lines.join("\n") : logs.isLoading ? "…" : t("Nothing matches.")}
        </pre>
      )}
      <ErrorBox error={logs.error} />
    </section>
  );
}
