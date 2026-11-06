// One PR: what it is, what the service did and plans to do, its internal priority (with
// an override), and whether it can merge. The agent opened here talks about this PR.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Evaluation, type Me, type PrDetail, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { agentName, AgentPicker, useAgentChoice } from "../agentPicker";
import { Empty, ErrorBox, Labels, PageHead, Pre, Section, Status, Tier, timeOf, useAct, When } from "../components";
import { REVIEW_STATES, usePrs } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { FixButton } from "../proposal";
import { ReproduceButton } from "../reproduce";
import { PrWork } from "../jobs";
import { openJob } from "../openJob";
import { Skeleton, Tabs, useTab, useToast } from "../ui";

const TIERS = ["P0", "P1", "P2", "P3"];
type TimelineRow = PrDetail["actions"][number];

export default function PrPage({ me }: { me: Me }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const { owner, repo, number } = useParams();
  const full = `${owner}/${repo}`;
  const base = `/api/prs/${full}/${number}`;
  const detail = useQuery({ queryKey: ["pr", owner, repo, number], queryFn: () => api.get<PrDetail>(base) });
  const rows = usePrs();
  const row = rows.data?.find((p) => p.repo === full && String(p.number) === number);
  const keys = [["pr"], ["prs"]];
  const claim = useAct(() => api.post(`${base}/claim`), keys);
  const unclaim = useAct(() => api.del(`${base}/claim`), keys);
  const [reviewer, setReviewer] = useAgentChoice("review");
  const review = useAct(() => api.post<ReviewStart>(`${base}/review`, { agent: reviewer || null }), [...keys, ["proposals"]]);
  const arm = useAct(() => api.post(`${base}/merge`), keys);
  const disarm = useAct(() => api.del(`${base}/merge`), keys);
  const d = detail.data;

  const tabs = [
    { id: "overview", label: t("Overview") },
    { id: "files", label: t("Files"), count: d?.pr.files.length },
    { id: "timeline", label: t("Timeline"), count: d?.actions.length },
    { id: "reviews", label: t("Reviews"), count: d?.reviews.length },
    { id: "ci", label: t("CI"), count: d?.ci.length },
  ];
  const [tab, setTab] = useTab(tabs);

  if (detail.isLoading) {
    return (
      <div className="card">
        <Skeleton rows={8} />
      </div>
    );
  }
  if (detail.error || !d) return <ErrorBox error={detail.error} />;
  const pr = d.pr;
  const claimedBy = row?.claimed_by ?? null;
  const mutationError = claim.error || unclaim.error || review.error || arm.error || disarm.error;
  const job = row?.review_activity.job;
  const reviewing = !!job && ["queued", "running", "waiting"].includes(job.status);
  const reviewingText = reviewing
    ? `${t("reviewing…")} ${agentName(job?.agent)}${job?.by ? ` · ${job.by === me.login ? t("you") : job.by}` : ""}`
    : "";
  // The fix endpoint works from a review action, drafted or posted, that has findings.
  const withFindings = d.actions.filter(
    (a) => a.action.kind === "review.post" && ((a.action.payload.result as { findings?: unknown[] } | undefined)?.findings?.length ?? 0) > 0,
  );
  const latestReview = withFindings.length
    ? withFindings.reduce((a, b) => ((timeOf(a.action.created_at) ?? 0) >= (timeOf(b.action.created_at) ?? 0) ? a : b)).action.id
    : null;
  const adds = pr.files.reduce((n, f) => n + f.additions, 0);
  const dels = pr.files.reduce((n, f) => n + f.deletions, 0);

  return (
    <>
      <PageHead
        crumbs={[
          { to: "/prs", label: t("Pull requests") },
          { to: `/prs?f.repo=${encodeURIComponent(full)}`, label: full },
        ]}
        title={
          <>
            {pr.title} <span className="muted" style={{ fontWeight: 400 }}>#{pr.number}</span>
          </>
        }
        sub={
          <span className="row" style={{ gap: 8 }}>
            <Status value={pr.state.toLowerCase()} />
            {pr.is_draft && <Status value="draft" label={t("draft")} />}
            <span>
              {pr.author} {t("wants to merge into")} <code>{pr.base_ref}</code> {t("from")} <code>{pr.head_sha.slice(0, 7)}</code>
            </span>
            <Labels labels={pr.labels} max={6} />
          </span>
        }
        actions={
          <>
            <button className="agent" onClick={() => agent.open()}>
              <Icon name="sparkles" />
              {t("Ask agent")}
            </button>
            <AgentPicker value={reviewer} onChange={setReviewer} />
            <button
              className="primary"
              disabled={review.isPending}
              title={reviewing ? reviewingText : undefined}
              onClick={() =>
                reviewing && job?.id
                  ? openJob(job.id)
                  : review.mutate(undefined, {
                  onSuccess: (o) =>
                    toast(
                      o.run === "review" || o.run === "followup"
                        ? {
                            title: t("Review requested"),
                            body: t("Watch the agent work; the draft comes back to you to edit and post."),
                            icon: "pr",
                            action: o.event ? { label: t("Watch"), onClick: () => openJob(o.event as string) } : undefined,
                          }
                        : o.run === "skip"
                          ? { title: t("Nothing to review"), body: o.reason, icon: "check" }
                          : {
                              title: o.run === "joined" ? t("Already being reviewed") : t("Already drafted"),
                              body: `${o.by ?? "—"} · ${agentName(o.agent) || t("Default agent")}`,
                              icon: "pr",
                            },
                    ),
                })
              }
            >
              <Icon name="pr" size={15} />
              {reviewing ? t("Reviewing…") : d.review_state.reviewed_sha ? t("Follow up") : t("Review")}
            </button>
            {latestReview && <FixButton repo={full} number={pr.number} review={latestReview} align="right" />}
            <ReproduceButton repo={full} number={pr.number} />
            {claimedBy === me.login ? (
              <button onClick={() => unclaim.mutate(undefined)}>{t("Unclaim")}</button>
            ) : (
              <button onClick={() => claim.mutate(undefined)} title={claimedBy ? `${t("Claimed by")} ${claimedBy}` : undefined}>
                {claimedBy ? t("Take over") : t("Claim")}
              </button>
            )}
            <a className="button" href={pr.url} target="_blank" rel="noreferrer">
              GitHub <Icon name="external" size={13} />
            </a>
          </>
        }
      />
      <ErrorBox error={mutationError} />

      <div className="split">
        <div className="stack" style={{ gap: 16, minWidth: 0 }}>
          <PrWork repo={full} number={pr.number} me={me.login} />
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
          {tab === "overview" && <Overview d={d} />}
          {tab === "files" && <Files d={d} />}
          {tab === "timeline" && <Timeline d={d} />}
          {tab === "reviews" && (
            <Section title={t("Reviews by the service")}>
              {d.reviews.length ? (
                <div className="stack">
                  {d.reviews.map((r, i) => (
                    <details key={i} open={i === 0}>
                      <summary className="small">
                        {r.kind} {t("at")} <code>{r.sha.slice(0, 7)}</code> · <When at={r.created_at} />
                      </summary>
                      <div style={{ marginTop: 8 }}>
                        <Pre text={String(r.result.body ?? JSON.stringify(r.result, null, 2))} />
                      </div>
                    </details>
                  ))}
                </div>
              ) : (
                <Empty icon="pr">{t("The service has not posted a review on this PR.")}</Empty>
              )}
            </Section>
          )}
          {tab === "ci" && (
            <Section title={t("CI diagnoses")}>
              {d.ci.length ? (
                <div className="list">
                  {d.ci.map((o, i) => (
                    <div key={i} className="list-row" style={{ alignItems: "flex-start" }}>
                      <Status value={o.category} />
                      <span className="main">
                        <span className="t">{o.workflow ? `${o.workflow} / ${o.check_name}` : o.check_name}</span>
                        <span className="small" style={{ whiteSpace: "normal", display: "block" }}>
                          {o.explanation}
                        </span>
                        <span className="meta">
                          {o.signature && <code>{o.signature}</code>} {o.rerun ? `· ${t("rerun requested")}` : ""} · <When at={o.created_at} />
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty icon="activity">{t("No failed jobs diagnosed on this PR.")}</Empty>
              )}
            </Section>
          )}
        </div>

        <aside className="side">
          <EvaluationCard base={base} evaluation={d.evaluation} />
          <Section
            title={t("Merge")}
            aside={
              d.arm ? (
                <button className="danger sm" onClick={() => disarm.mutate(undefined)}>
                  {t("Cancel merge")}
                </button>
              ) : (
                <button className="sm" onClick={() => arm.mutate(undefined)} title={t("Merge automatically once every check passes")}>
                  <Icon name="merge" size={14} />
                  {t("Arm merge")}
                </button>
              )
            }
          >
            {d.gate ? (
              <div className="stack">
                <span className="small">
                  {t("Outcome")}: <strong>{d.gate.outcome.replace(/_/g, " ")}</strong>
                  {d.arm && (
                    <span className="muted">
                      {" "}
                      · {t("armed by")} {d.arm.armed_by}
                    </span>
                  )}
                </span>
                <ul className="checklist small">
                  {d.gate.items.map((i) => (
                    <li key={i.name}>
                      <span className={i.ok ? "check-ok" : i.pending ? "check-wait" : "check-bad"}>
                        <Icon name={i.ok ? "check" : i.pending ? "clock" : "x"} size={14} />
                      </span>
                      <span>
                        {i.name.replace(/_/g, " ")}
                        {i.detail && <span className="muted">: {i.detail}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <span className="small muted">{t("Arm a merge to see what it waits for. The service merges once every item passes.")}</span>
            )}
          </Section>
          <Section title={t("Details")}>
            <dl className="props">
              <dt>{t("Review state")}</dt>
              <dd>{t(REVIEW_STATES[d.review_state.state] ?? d.review_state.state)}</dd>
              <dt>{t("Last reviewed")}</dt>
              <dd>{d.review_state.reviewed_sha ? <code>{d.review_state.reviewed_sha.slice(0, 7)}</code> : "—"}</dd>
              <dt>{t("Claimed by")}</dt>
              <dd>{claimedBy ?? "—"}</dd>
              <dt>{t("Changes")}</dt>
              <dd>
                {pr.files.length} {t("files")} · <span style={{ color: "var(--ok)" }}>+{adds}</span> <span style={{ color: "var(--bad)" }}>−{dels}</span>
              </dd>
              <dt>{t("Hygiene")}</dt>
              <dd>
                {d.hygiene?.results?.length ? (
                  <ul className="checklist small">
                    {d.hygiene.results
                      .filter((r) => !r.skipped)
                      .map((r) => (
                        <li key={r.id} title={r.details.join(", ")}>
                          <span className={r.passed ? "check-ok" : r.blocking ? "check-bad" : "check-wait"}>
                            <Icon name={r.passed ? "check" : "x"} size={13} />
                          </span>
                          <span>{r.summary}</span>
                        </li>
                      ))}
                  </ul>
                ) : (
                  <span className="muted">{t("not checked yet")}</span>
                )}
              </dd>
            </dl>
          </Section>
        </aside>
      </div>
    </>
  );
}

function Overview({ d }: { d: PrDetail }) {
  const t = useT();
  const ev = d.evaluation;
  return (
    <>
      {ev?.summary && (
        <Section title={t("Summary")} hint={t("internal")}>
          <div className="stack small">
            <span style={{ fontSize: 14 }}>{ev.summary}</span>
            {ev.rationale && <span className="muted">{ev.rationale}</span>}
          </div>
        </Section>
      )}
      <Section title={t("Description")}>
        {d.pr.body?.trim() ? (
          <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 13.5, maxHeight: 520, overflow: "auto" }}>{d.pr.body}</div>
        ) : (
          <span className="muted small">{t("No description.")}</span>
        )}
      </Section>
      <Section title={t("Checks")}>
        {d.pr.checks.length ? (
          <div className="list">
            {d.pr.checks.map((c) => (
              <div key={c.id} className="list-row">
                <Status value={c.conclusion ?? c.status} />
                <span className="main">
                  <span className="t">{c.name}</span>
                  {c.workflow && <span className="meta">{c.workflow}</span>}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <span className="muted small">{t("No checks reported.")}</span>
        )}
      </Section>
    </>
  );
}

function Files({ d }: { d: PrDetail }) {
  const t = useT();
  type F = PrDetail["pr"]["files"][number];
  const list = useList<F>({
    rows: d.pr.files,
    key: (f) => f.path,
    search: (f) => f.path,
    sorts: [
      { id: "path", label: t("Path"), value: (f) => f.path },
      { id: "changes", label: t("Lines changed"), value: (f) => f.additions + f.deletions, descFirst: true },
      { id: "add", label: t("Additions"), value: (f) => f.additions, descFirst: true },
      { id: "del", label: t("Deletions"), value: (f) => f.deletions, descFirst: true },
    ],
    defaultSort: "path",
    groups: [{ id: "dir", label: t("Directory"), value: (f) => f.path.split("/").slice(0, -1).join("/") || "/" }],
    pageSize: 100,
    param: "files",
  });
  const columns: Column<F>[] = [
    { id: "path", header: t("Path"), sort: "path", cell: (f) => <span className="mono">{f.path}</span> },
    { id: "add", header: "+", sort: "add", className: "num", cell: (f) => <span style={{ color: "var(--ok)" }}>{f.additions}</span> },
    { id: "del", header: "−", sort: "del", className: "num", cell: (f) => <span style={{ color: "var(--bad)" }}>{f.deletions}</span> },
    {
      id: "bar",
      header: t("Size"),
      sort: "changes",
      cell: (f) => {
        const total = f.additions + f.deletions;
        const max = Math.max(1, ...d.pr.files.map((x) => x.additions + x.deletions));
        return (
          <span title={`${total}`} style={{ display: "inline-flex", width: 100, height: 6, borderRadius: 3, overflow: "hidden", background: "var(--surface-3)", gap: 2 }}>
            <span style={{ width: `${(f.additions / max) * 100}%`, background: "var(--ok)", borderRadius: 3 }} />
            <span style={{ width: `${(f.deletions / max) * 100}%`, background: "var(--bad)", borderRadius: 3 }} />
          </span>
        );
      },
    },
  ];
  return (
    <>
      <ListToolbar list={list} placeholder={t("Filter files")} />
      <DataTable list={list} columns={columns} empty={t("No files.")} />
    </>
  );
}

function Timeline({ d }: { d: PrDetail }) {
  const t = useT();
  const list = useList<TimelineRow>({
    rows: d.actions,
    key: (a) => a.action.id,
    search: (a) => `${a.action.kind} ${a.action.rationale} ${a.error ?? ""} ${a.decision?.rule ?? ""}`,
    facets: [
      { id: "kind", label: t("Action"), values: (a) => a.action.kind },
      { id: "status", label: t("Status"), values: (a) => a.status },
    ],
    sorts: [
      { id: "when", label: t("Time"), value: (a) => timeOf(a.action.created_at), descFirst: true },
      { id: "kind", label: t("Action"), value: (a) => a.action.kind },
      { id: "status", label: t("Status"), value: (a) => a.status },
    ],
    defaultSort: "-when",
    param: "tl",
  });
  const columns: Column<TimelineRow>[] = [
    { id: "when", header: t("When"), sort: "when", cell: (a) => <When at={a.action.created_at} />, width: 100 },
    { id: "kind", header: t("Action"), sort: "kind", cell: (a) => <span className="mono">{a.action.kind}</span> },
    { id: "status", header: t("Status"), sort: "status", cell: (a) => <Status value={a.status} /> },
    {
      id: "decision",
      header: t("Decision"),
      className: "hide-narrow small",
      cell: (a) => (a.decision ? `${a.decision.kind} · ${a.decision.rule}` : "—"),
    },
    {
      id: "proposal",
      header: t("Proposal"),
      cell: (a) =>
        a.proposal ? (
          <Link to="/inbox">
            <Status value={a.proposal.state} />
          </Link>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      id: "why",
      header: t("Why / error"),
      className: "small",
      cell: (a) => (a.error ? <span style={{ color: "var(--bad)" }}>{a.error}</span> : a.action.rationale),
    },
  ];
  return (
    <>
      <ListToolbar list={list} placeholder={t("Search the timeline")} />
      <DataTable list={list} columns={columns} empty={t("The service has not acted on this PR yet.")} />
    </>
  );
}

function ScoreBar({ name, value }: { name: string; value: number }) {
  return (
    <div className="row" style={{ gap: 8, flexWrap: "nowrap" }} title={`${name}: ${value}`}>
      <span className="small" style={{ width: 104, flexShrink: 0 }}>
        {name.replace(/_/g, " ")}
      </span>
      <span style={{ flex: 1, height: 6, borderRadius: 3, background: "var(--surface-3)", overflow: "hidden" }}>
        <span style={{ display: "block", width: `${Math.max(0, Math.min(10, value)) * 10}%`, height: "100%", borderRadius: 3, background: "var(--accent)" }} />
      </span>
      <span className="small" style={{ width: 28, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        {value}
      </span>
    </div>
  );
}

function EvaluationCard({ base, evaluation }: { base: string; evaluation: Evaluation | null }) {
  const t = useT();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [scores, setScores] = useState<Record<string, string>>({});
  const [boost, setBoost] = useState("");
  const [tier, setTier] = useState("");
  const [reason, setReason] = useState("");
  useEffect(() => {
    setScores(Object.fromEntries(Object.entries(evaluation?.scores ?? {}).map(([k, v]) => [k, String(v)])));
    setBoost(evaluation ? String(evaluation.manual_boost) : "");
    setTier(evaluation?.tier_override ?? "");
  }, [evaluation]);
  const override = useAct(
    () => {
      const body: Record<string, unknown> = { reason };
      const parsed = Object.fromEntries(Object.entries(scores).map(([k, v]) => [k, Number(v)]));
      if (Object.keys(parsed).length) body.scores = parsed;
      if (boost !== "") body.manual_boost = Number(boost);
      body.tier = tier;
      return api.post<Evaluation>(`${base}/override`, body);
    },
    [["pr"], ["prs"]],
  );

  return (
    <Section
      title={t("Priority")}
      hint={t("internal")}
      aside={
        evaluation ? (
          <span className="row" style={{ gap: 6 }}>
            <Tier tier={evaluation.tier} />
            <strong style={{ fontVariantNumeric: "tabular-nums" }}>{evaluation.total.toFixed(1)}</strong>
          </span>
        ) : null
      }
    >
      {evaluation ? (
        <div className="stack small" style={{ gap: 8 }}>
          {Object.entries(evaluation.scores).map(([k, v]) => (
            <ScoreBar key={k} name={k} value={v} />
          ))}
          <span className="muted">
            {t("base")} {evaluation.base.toFixed(1)} + {t("goals")} {evaluation.boost.toFixed(1)} + {t("manual")} {evaluation.manual_boost.toFixed(1)}
            {evaluation.matched_goals.length > 0 && ` · ${evaluation.matched_goals.join(", ")}`}
          </span>
          {evaluation.override_by && (
            <span className="muted">
              {t("Adjusted by")} {evaluation.override_by}: {evaluation.override_reason}
            </span>
          )}
        </div>
      ) : (
        <span className="small muted">{t("Not evaluated yet.")}</span>
      )}
      {!open ? (
        <button className="ghost sm" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>
          <Icon name="refresh" size={14} />
          {t("Adjust priority")}
        </button>
      ) : (
        <form
          className="stack"
          style={{ marginTop: 12, gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            override.mutate(undefined, {
              onSuccess: () => {
                setOpen(false);
                setReason("");
                toast({ title: t("Priority adjusted"), body: t("The calibration agent will reply in your conversations."), icon: "check" });
              },
            });
          }}
        >
          <div className="filters">
            {Object.keys(scores).map((dim) => (
              <label key={dim} className="field">
                {dim.replace(/_/g, " ")}
                <input type="number" min={0} max={10} step={0.5} value={scores[dim]} onChange={(e) => setScores({ ...scores, [dim]: e.target.value })} style={{ width: 76 }} />
              </label>
            ))}
            <label className="field">
              {t("manual boost")}
              <input type="number" step={0.5} value={boost} onChange={(e) => setBoost(e.target.value)} style={{ width: 76 }} />
            </label>
            <label className="field">
              {t("Tier")}
              <select value={tier} onChange={(e) => setTier(e.target.value)}>
                <option value="">{t("computed")}</option>
                {TIERS.map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </label>
          </div>
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Why the priority changes (required)")} />
          <div className="row">
            <button className="primary sm" disabled={!reason.trim() || override.isPending}>
              {t("Save")}
            </button>
            <button type="button" className="ghost sm" onClick={() => setOpen(false)}>
              {t("Cancel")}
            </button>
          </div>
          <ErrorBox error={override.error} />
        </form>
      )}
    </Section>
  );
}

