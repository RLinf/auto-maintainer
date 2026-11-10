// One PR: what it is, what the service did and plans to do, its internal priority (with
// an override), and whether it can merge. The agent opened here talks about this PR.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Evaluation, type Me, type PrDetail, type QuickQuality, type ReviewQuality, type ReviewStart, type RootCause, type RunCiPlan } from "../api";
import { agentName, useAgentChoice, useAgentOptions } from "../agentPicker";
import {
  Empty,
  ErrorBox,
  Labels,
  PageHead,
  Pre,
  prPath,
  Quality,
  QUALITY_BANDS,
  QualityChecks,
  qualityLabel,
  plainText,
  qualityStale,
  reasonIn,
  showsQuick,
  Section,
  Status,
  Tier,
  timeOf,
  useAct,
  When,
} from "../components";
import { evaluationToast, qualityToast, REVIEW_STATES, usePrs, useReevaluate, useScoreQuality } from "../data";
import { useLang, useT } from "../i18n";
import { Icon } from "../icons";
import { Markdown } from "../markdown";
import { CommentOnDiff, parseDiff, usePrDiff } from "../diffview";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { ApproveButton, FixForm, MergeBaseButton, MergeBaseForm, type InlineComment } from "../proposal";
import { ReproduceForm } from "../reproduce";
import { failedChecks, FixCiButton, FixCiForm } from "../fixCi";
import { PrPeopleCards } from "../assignees";
import { CodeOwnersCard } from "../codeOwners";
import { ReviewProgressPanel } from "../progress";
import { ACTIVE, PrWork, usePrWork } from "../jobs";
import { openJob } from "../openJob";
import { activePrActionPanel, describeRunCi, prMenuItems, type PrActionPanel } from "../prActions";
import { MenuItem, Popover, Skeleton, Tabs, useTab, useToast } from "../ui";
import { TranslateReview } from "../translate";
import { forgeOf } from "../forges";

const TIERS = ["P0", "P1", "P2", "P3"];
type TimelineRow = PrDetail["actions"][number];

export default function PrPage({ me }: { me: Me }) {
  const t = useT();
  const { lang } = useLang();
  const toast = useToast();
  // The repository's name may hold more slashes (forge:group/subgroup/project); the
  // PR's number is the last part.
  const rest = useParams()["*"] ?? "";
  const split = rest.lastIndexOf("/");
  const full = rest.slice(0, split);
  const number = rest.slice(split + 1);
  const base = `/api/prs/${full}/${number}`;
  const detail = useQuery({ queryKey: ["pr", full, number], queryFn: () => api.get<PrDetail>(base) });
  const rows = usePrs();
  const row = rows.data?.find((p) => p.repo === full && String(p.number) === number);
  const keys = [["pr"], ["prs"]];
  const [reviewer, setReviewer] = useAgentChoice("review");
  const agentOpts = useAgentOptions(reviewer, "review");
  const review = useAct(() => api.post<ReviewStart>(`${base}/review`, { agent: agentOpts.current || null }), [...keys, ["proposals"]]);
  const arm = useAct(() => api.post(`${base}/merge`), keys);
  const disarm = useAct(() => api.del(`${base}/merge`), keys);
  const d = detail.data;

  const tabs = [
    { id: "overview", label: t("Overview") },
    { id: "progress", label: t("Review progress") },
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
  const canPush = !!me.can_post_as_me;
  const failed = failedChecks(pr.checks);
  const forgeName = forgeOf(full).terms.forge_name;
  const openOnForge =
    forgeName === "GitHub" || forgeName === "GitLab"
      ? t(`Open on ${forgeName}`)
      : lang === "zh"
        ? `在 ${forgeName} 打开`
        : `Open on ${forgeName}`;
  const mutationError = review.error || arm.error || disarm.error;
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
  const ciControls =
    describeRunCi(d.run_ci.state, canPush).inline !== "none" || (pr.state === "OPEN" && failed.length > 0) ? (
      <span className="row" style={{ gap: 6 }}>
        <RunCiButton repo={full} number={pr.number} plan={d.run_ci} canPush={canPush} size="sm" />
        {pr.state === "OPEN" && <FixCiButton repo={full} number={pr.number} failed={failed} canPush={canPush} size="sm" />}
      </span>
    ) : null;

  return (
    <>
      <PageHead
        crumbs={[
          { to: "/prs", label: t("Pull requests") },
          { to: `/prs?repo=${encodeURIComponent(full)}`, label: full },
        ]}
        title={
          <>
            {pr.title}{" "}
            <a className="pr-number" href={pr.url} target="_blank" rel="noreferrer" title={openOnForge}>
              #{pr.number}
              <Icon name="external" size={15} />
            </a>
          </>
        }
        sub={
          <span className="stack" style={{ gap: 6 }}>
            <span className="row" style={{ gap: 8 }}>
              <Status value={pr.state.toLowerCase()} />
              {pr.is_draft && <Status value="draft" label={t("draft")} />}
              {pr.mergeable === "CONFLICTING" && <Status value="failed" label={`${t("conflicts with")} ${pr.base_ref}`} />}
              {(d.quality || d.quick_quality) && <Quality quality={d.quality} quick={d.quick_quality} head={pr.head_sha} />}
              <span>
                {pr.author} {t("wants to merge into")} <code>{pr.base_ref}</code> {t("from")} <code>{pr.head_sha.slice(0, 7)}</code>
              </span>
            </span>
            {pr.labels.length > 0 && <Labels labels={pr.labels} max={8} />}
          </span>
        }
        actions={
          <>
            <div className="split-button">
              <button
                className="primary"
                disabled={review.isPending}
                title={
                  reviewing
                    ? reviewingText
                    : agentOpts.current
                      ? `${t("Which agent does it")}: ${agentOpts.label(agentOpts.current)}`
                      : agentOpts.primary
                        ? `${t("Which agent does it")}: ${agentOpts.label(agentOpts.primary)} (${t("default")})`
                        : undefined
                }
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
                {!reviewing && agentOpts.current && <span className="agent-hint">· {agentName(agentOpts.current)}</span>}
              </button>
              <Popover
                align="right"
                label={t("Which agent does it")}
                trigger={(open, toggle) => (
                  <button
                    type="button"
                    className={`primary caret${open ? " on" : ""}`}
                    aria-label={t("Which agent does it")}
                    title={t("Which agent does it")}
                    aria-expanded={open}
                    onClick={toggle}
                  >
                    <Icon name="chevronDown" size={14} />
                  </button>
                )}
              >
                {(close) => (
                  <>
                    <div className="menu-label">{t("Which agent does it")}</div>
                    <MenuItem
                      checked={!agentOpts.current}
                      onClick={() => {
                        setReviewer("");
                        close();
                      }}
                    >
                      {agentOpts.primary ? `${agentOpts.label(agentOpts.primary)} (${t("default")})` : t("Default agent")}
                    </MenuItem>
                    {agentOpts.others.map((a) => (
                      <MenuItem
                        key={a.name}
                        checked={agentOpts.current === a.name}
                        onClick={() => {
                          setReviewer(a.name);
                          close();
                        }}
                      >
                        {agentOpts.label(a.name)}
                      </MenuItem>
                    ))}
                  </>
                )}
              </Popover>
            </div>
            {pr.state === "OPEN" && (
              <ApproveButton
                repo={full}
                number={pr.number}
                head={pr.head_sha}
                approved={myApproval(pr.reviews ?? [], me.login, pr.head_sha)}
                own={pr.author.toLowerCase() === me.login.toLowerCase()}
                canPost={canPush}
              />
            )}
            <PrActionsButton
              repo={full}
              pr={pr}
              plan={d.run_ci}
              latestReview={latestReview}
              failed={failed}
              canPush={canPush}
              openOnForge={openOnForge}
            />
            <RefreshButton base={base} />
          </>
        }
      />
      <ErrorBox error={mutationError} />

      <div className="split">
        <div className="stack" style={{ gap: 16, minWidth: 0 }}>
          <PrWork repo={full} number={pr.number} me={me.login} />
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
          {tab === "overview" && <Overview d={d} ciActions={ciControls} />}
          {tab === "progress" && <ReviewProgressPanel repo={full} number={pr.number} me={me.login} />}
          {tab === "files" && <Files d={d} />}
          {tab === "timeline" && <Timeline d={d} />}
          {tab === "reviews" && <ReviewsTab d={d} repo={full} number={pr.number} />}
          {tab === "ci" && (
            <Section title={t("CI diagnoses")} aside={ciControls}>
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
                        {o.root_cause && <RootCauseView cause={o.root_cause} prUrl={d.pr.url} sha={o.sha} />}
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
          <PrPeopleCards repo={full} number={pr.number} author={pr.author} assignees={d.assignees ?? []} me={me.login} />
          <CodeOwnersCard repo={full} number={pr.number} head={pr.head_sha} />
          <QualityCard
            repo={full}
            number={pr.number}
            quality={d.quality}
            quick={d.quick_quality}
            head={pr.head_sha}
            reviewed={d.actions.some((a) => a.action.kind === "review.post")}
          />
          <EvaluationCard base={base} repo={full} number={pr.number} evaluation={d.evaluation} />
          <Section
            title={t("Merge")}
            aside={
              <span className="row" style={{ gap: 6 }}>
                {pr.state === "OPEN" && (
                  <MergeBaseButton
                    repo={full}
                    number={pr.number}
                    base={pr.base_ref}
                    conflicting={pr.mergeable === "CONFLICTING"}
                    canPush={canPush}
                    size="sm"
                    quiet={pr.mergeable !== "CONFLICTING"}
                  />
                )}
                {d.arm ? (
                  <button className="danger sm" onClick={() => disarm.mutate(undefined)}>
                    {t("Cancel merge")}
                  </button>
                ) : (
                  <button className="sm" onClick={() => arm.mutate(undefined)} title={t("Merge automatically once every check passes")}>
                    <Icon name="merge" size={14} />
                    {t("Arm merge")}
                  </button>
                )}
              </span>
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
                {d.merge_queue && d.merge_queue.order.length > 1 && <MergeQueueLine q={d.merge_queue} number={pr.number} repo={full} />}
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

/** Whether ``login``'s newest review on GitHub approves ``head``. */
function myApproval(reviews: NonNullable<PrDetail["pr"]["reviews"]>, login: string, head: string): boolean {
  const mine = reviews.filter((r) => r.author.toLowerCase() === login.toLowerCase() && r.state !== "COMMENTED");
  const last = mine[mine.length - 1];
  return !!last && last.state === "APPROVED" && !!last.commit_sha && (last.commit_sha.startsWith(head) || head.startsWith(last.commit_sha));
}

/** A review body as stored: an older renderer doubled a skill's own heading marks. */
function reviewBody(body: string): string {
  return body.replace(/^(#{1,6})\s+#{1,6}\s+/gm, "$1 ");
}

/** The reviews the service posted on the PR, newest first: the main comment as GitHub
 *  renders it, then each inline comment on the code it is about. */
function ReviewsTab({ d, repo, number }: { d: PrDetail; repo: string; number: number }) {
  const t = useT();
  const [open, setOpen] = useState<number>(0);
  const commentsOf = (actionId: string): InlineComment[] => {
    const record = d.actions.find((a) => a.action.id === actionId);
    const comments = record?.action.payload.comments;
    return Array.isArray(comments) ? (comments as InlineComment[]) : [];
  };
  const anyComments = d.reviews.some((r) => commentsOf(r.action_id).length > 0);
  const diff = usePrDiff(repo, number, anyComments);
  const files = useMemo(() => (diff.data ? parseDiff(diff.data.diff) : null), [diff.data]);
  if (!d.reviews.length) {
    return (
      <Section title={t("Reviews by the service")}>
        <Empty icon="pr">{t("The service has not posted a review on this PR.")}</Empty>
      </Section>
    );
  }
  return (
    <Section title={t("Reviews by the service")}>
      <div className="stack" style={{ gap: 12 }}>
        {d.reviews.map((r, i) => {
          const comments = commentsOf(r.action_id);
          return (
            <details key={i} open={open === i} onToggle={(e) => (e.currentTarget.open ? setOpen(i) : open === i && setOpen(-1))}>
              <summary className="small">
                {t(r.kind === "followup" ? "Follow-up review" : "Review")} {t("at")} <code>{r.sha.slice(0, 7)}</code> · <When at={r.created_at} />
                {comments.length > 0 && ` · ${comments.length} ${t("inline comments")}`}
              </summary>
              <div className="stack" style={{ marginTop: 10, gap: 12 }}>
                {typeof r.result.body === "string" ? (
                  <Markdown text={reviewBody(r.result.body)} className="posted" />
                ) : (
                  <Pre text={JSON.stringify(r.result, null, 2)} />
                )}
                {typeof r.result.body === "string" && (
                  <TranslateReview actionId={r.action_id} places={comments.map((c) => `${c.path}:${c.line}`)} />
                )}
                {comments.map((c, j) => (
                  <CommentOnDiff
                    key={`${c.path}:${c.line}:${j}`}
                    files={files}
                    loading={diff.isLoading}
                    path={c.path}
                    line={c.line}
                    start={c.start_line}
                    side={c.side}
                    outdated={t("This line is no longer in the PR's diff.")}
                  >
                    <Markdown text={c.body} className="posted" />
                  </CommentOnDiff>
                ))}
              </div>
            </details>
          );
        })}
      </div>
    </Section>
  );
}

function Overview({ d, ciActions }: { d: PrDetail; ciActions?: ReactNode }) {
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
          <Markdown text={d.pr.body} className="pr-body" />
        ) : (
          <span className="muted small">{t("No description.")}</span>
        )}
      </Section>
      <Section title={t("Checks")} aside={ciActions}>
        {d.pr.checks.length ? (
          <div className="list">
            {d.pr.checks.map((c) => (
              <div key={c.id} className="list-row">
                <Status
                  value={c.conclusion ?? c.status}
                  label={c.conclusion === "action_required" ? t("awaiting approval") : undefined}
                />
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
    { id: "path", primary: true, header: t("Path"), sort: "path", cell: (f) => <span className="mono">{f.path}</span> },
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
    { id: "kind", primary: true, header: t("Action"), sort: "kind", cell: (a) => <span className="mono">{a.action.kind}</span> },
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

const ORIGIN: Record<RootCause["origin"], { label: string; status: string }> = {
  change: { label: "caused by this PR", status: "failed" },
  base: { label: "fails without this PR too", status: "waiting" },
  infra: { label: "CI infrastructure", status: "waiting" },
  flaky: { label: "flaky", status: "cancelled" },
  unclear: { label: "unclear", status: "cancelled" },
};

/** What the root-cause agent found for a failed job: what fails, why, where (linked to
 * the file at that commit), the smallest fix, and the log lines that show it. */
function RootCauseView({ cause, prUrl, sha }: { cause: RootCause; prUrl: string; sha: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const base = prUrl.split("/pull/")[0];
  const origin = ORIGIN[cause.origin];
  return (
    <div className="root-cause">
      <div className="row" style={{ gap: 6 }}>
        <strong className="small">{t("Root cause")}</strong>
        <Status value={origin.status} label={t(origin.label)} />
        <code className="small">{cause.failing}</code>
      </div>
      <span className="small" style={{ whiteSpace: "normal" }}>
        {cause.cause}
      </span>
      {cause.culprits.length > 0 && (
        <ul className="small">
          {cause.culprits.map((c) => (
            <li key={`${c.path}:${c.line}`}>
              <a href={`${base}/blob/${sha}/${c.path}${c.line ? `#L${c.line}` : ""}`} target="_blank" rel="noreferrer">
                <code>{c.line ? `${c.path}:${c.line}` : c.path}</code>
              </a>
              {c.why ? ` · ${c.why}` : ""}
            </li>
          ))}
        </ul>
      )}
      {cause.fix && (
        <span className="small" style={{ whiteSpace: "normal" }}>
          <strong>{t("Suggested fix")}:</strong> {cause.fix}
        </span>
      )}
      {cause.evidence.length > 0 && (
        <>
          <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setOpen(!open)}>
            <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
            {t("Log lines")}
          </button>
          {open && <Pre text={cause.evidence.join("\n")} />}
        </>
      )}
    </div>
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

/** Read this PR from GitHub now instead of waiting for the next poll. */
function RefreshButton({ base }: { base: string }) {
  const t = useT();
  const toast = useToast();
  const refresh = useAct(() => api.post<{ changes: string[] }>(`${base}/refresh`), [["pr"], ["prs"]]);
  return (
    <button
      className="icon"
      title={t("Read this PR from GitHub now")}
      aria-label={t("Read this PR from GitHub now")}
      disabled={refresh.isPending}
      onClick={() =>
        refresh.mutate(undefined, {
          onSuccess: (r) => toast({ title: r.changes.length ? t("Updated from GitHub") : t("Already up to date"), icon: "refresh" }),
          onError: (e) => toast({ title: t("Could not read it from GitHub"), body: e instanceof Error ? e.message : String(e), icon: "x" }),
        })
      }
    >
      <Icon name="refresh" size={15} />
    </button>
  );
}

/** The PR's quality: the accurate score its review gave, and the quick score from its
 *  diff that reviewers choose by before a review; each with the agent's reason, and a
 *  button that has an agent score it afresh. */
function QualityCard({
  repo,
  number,
  quality,
  quick,
  head,
  reviewed,
}: {
  repo: string;
  number: number;
  quality: ReviewQuality | null;
  quick: QuickQuality | null;
  head: string;
  reviewed: boolean;
}) {
  const t = useT();
  const { lang } = useLang();
  const toast = useToast();
  const score = useScoreQuality();
  const client = useQueryClient();
  const work = usePrWork(repo, number).data ?? [];
  const scoring = work.find((j) => (j.playbook === "quality" || j.playbook === "quick_quality") && ACTIVE.has(j.status));
  const scoringId = scoring?.id;
  const [watched, setWatched] = useState<string | undefined>();
  useEffect(() => {
    // Read the PR again once the agent scoring it finishes, so the new score shows.
    if (scoringId) setWatched(scoringId);
    else if (watched) {
      setWatched(undefined);
      void client.invalidateQueries({ queryKey: ["pr"] });
      void client.invalidateQueries({ queryKey: ["prs"] });
    }
  }, [scoringId, watched, client]);
  const start = (quickly: boolean) =>
    score.mutate({ prs: [{ repo, number }], quick: quickly }, { onSuccess: (out) => toast(qualityToast(out, t)) });
  // The quick score leads before a review, and after new commits the review did not see.
  const isQuick = showsQuick(quality, quick, head);
  const main = isQuick ? quick : (quality ?? quick);
  const other = isQuick ? quality : quick;
  const moved = !!quality && qualityStale(quality, head);
  const bandsTitle = QUALITY_BANDS.map((b) => `${b.floor}+ ${t(b.label)}`).join(" · ");
  return (
    <Section
      title={t("Quality")}
      hint={t("internal")}
      aside={
        main ? (
          <span className={`pill quality q-${main.band}${isQuick ? " quick" : ""}`} title={bandsTitle}>
            {t(qualityLabel(main.band))}
          </span>
        ) : null
      }
    >
      {main ? (
        <div className={`stack small q-${main.band}`} style={{ gap: 8 }}>
          <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
            <span style={{ fontSize: 28, fontWeight: 700, lineHeight: 1, color: "var(--qc)", fontVariantNumeric: "tabular-nums" }}>{main.score}</span>
            <span className="muted">/ 100</span>
            <span className="muted">· {isQuick ? t("at a glance") : t("after review")}</span>
          </div>
          <div className="quality-meter" role="img" aria-label={`${main.score} / 100`} title={`${main.score} / 100 · ${bandsTitle}`}>
            <span style={{ width: `${main.score}%` }} />
          </div>
          <QualityChecks checks={main.checks} />
          {reasonIn(main, lang) && <Markdown text={reasonIn(main, lang)} className="quality-reason" />}
          <span className="muted">
            {!isQuick && quality ? (
              <>
                {t(
                  quality.rescored
                    ? quality.kind === "followup"
                      ? "scored again for the follow-up at"
                      : "scored again for the review at"
                    : quality.kind === "followup"
                      ? "judged in the follow-up at"
                      : "judged in the review at",
                )}{" "}
                <code>{quality.sha.slice(0, 7)}</code>
                {!quality.posted && ` · ${t("not posted")}`}
              </>
            ) : (
              <>
                {t("quick score from the diff at")} <code>{main.sha.slice(0, 7)}</code> ·{" "}
                {moved ? t("new commits since the review; a follow-up gives the accurate score") : t("a review gives the accurate score")}
              </>
            )}
            {qualityStale(main, head) && ` · ${t("new commits since")}`}
          </span>
          {other && (
            <span className="muted">
              {isQuick ? t("After review it scored") : t("At a glance it scored")}{" "}
              <span className={`pill quality q-${other.band}${isQuick ? "" : " quick"}`} title={plainText(reasonIn(other, lang))}>
                {other.score}
              </span>
              {isQuick && quality && (
                <>
                  {" "}
                  {t("at")} <code>{quality.sha.slice(0, 7)}</code>
                </>
              )}
            </span>
          )}
        </div>
      ) : (
        <span className="small muted">{t("An agent gives every open PR a quick score from its diff; its review gives the accurate one.")}</span>
      )}
      <div className="row" style={{ marginTop: 10, gap: 6 }}>
        {scoring ? (
          <button className="ghost sm" onClick={() => openJob(scoring.id)}>
            <span className="pulse" />
            {t("An agent is scoring its quality")}
          </button>
        ) : reviewed && !moved ? (
          <button className="ghost sm" disabled={score.isPending} onClick={() => start(false)} title={t("An agent judges the PR again by the repository's quality guide")}>
            <Icon name="sparkles" size={14} />
            {score.isPending ? t("Starting…") : quality ? t("Score quality again") : t("Score quality")}
          </button>
        ) : moved ? (
          <button className="ghost sm" disabled={score.isPending} onClick={() => start(true)} title={t("An agent scores the commits since the review quickly from the diff")}>
            <Icon name="sparkles" size={14} />
            {score.isPending ? t("Starting…") : t("Score the new commits")}
          </button>
        ) : (
          <button className="ghost sm" disabled={score.isPending} onClick={() => start(true)} title={t("An agent scores the PR quickly from its diff")}>
            <Icon name="sparkles" size={14} />
            {score.isPending ? t("Starting…") : quick ? t("Quick score again") : t("Quick score")}
          </button>
        )}
      </div>
      <ErrorBox error={score.error} />
    </Section>
  );
}

function EvaluationCard({ base, repo, number, evaluation }: { base: string; repo: string; number: number; evaluation: Evaluation | null }) {
  const t = useT();
  const toast = useToast();
  const again = useReevaluate();
  // The agent scoring this PR now, if one is: its line opens what it is doing.
  const scoring = (usePrWork(repo, number).data ?? []).find((j) => j.playbook === "evaluate" && ACTIVE.has(j.status));
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
      {scoring && (
        <div className="row small" style={{ marginTop: 10, gap: 6 }}>
          <span className="pulse" />
          <span className="muted">{t("An agent is scoring this PR")}</span>
          <button className="ghost sm" onClick={() => openJob(scoring.id)}>
            {t("Watch")}
          </button>
        </div>
      )}
      {!open ? (
        <div className="row" style={{ marginTop: 10, gap: 6 }}>
          <button className="ghost sm" onClick={() => setOpen(true)}>
            <Icon name="settings" size={14} />
            {t("Adjust priority")}
          </button>
          <button
            className="ghost sm"
            disabled={again.isPending}
            title={t("An agent scores every dimension again, with the repository's lessons as they are now")}
            onClick={() => again.mutate([{ repo, number }], { onSuccess: (out) => toast(evaluationToast(out, t)) })}
          >
            <Icon name="sparkles" size={14} />
            {again.isPending ? t("Starting…") : t("Score again")}
          </button>
        </div>
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
      <ErrorBox error={again.error} />
    </Section>
  );
}


/** Secondary PR actions in a single popover; actions that need options switch the panel
 *  inline instead of nesting popovers. */
function PrActionsButton({
  repo,
  pr,
  plan,
  latestReview,
  failed,
  canPush,
  openOnForge,
}: {
  repo: string;
  pr: PrDetail["pr"];
  plan: RunCiPlan;
  latestReview: string | null;
  failed: string[];
  canPush: boolean;
  openOnForge: string;
}) {
  const t = useT();
  return (
    <Popover
      align="right"
      label={t("More actions")}
      trigger={(open, toggle) => (
        <button
          type="button"
          className={open ? "on" : ""}
          aria-label={t("More actions")}
          title={t("More actions")}
          aria-expanded={open}
          onClick={toggle}
        >
          {t("Actions")}
          <Icon name="chevronDown" size={14} />
        </button>
      )}
    >
      {(close) => (
        <PrActionsMenu
          repo={repo}
          pr={pr}
          plan={plan}
          latestReview={latestReview}
          failed={failed}
          canPush={canPush}
          openOnForge={openOnForge}
          onClose={close}
        />
      )}
    </Popover>
  );
}

function PrActionsMenu({
  repo,
  pr,
  plan,
  latestReview,
  failed,
  canPush,
  openOnForge,
  onClose,
}: {
  repo: string;
  pr: PrDetail["pr"];
  plan: RunCiPlan;
  latestReview: string | null;
  failed: string[];
  canPush: boolean;
  openOnForge: string;
  onClose: () => void;
}) {
  const t = useT();
  const [panel, setPanel] = useState<PrActionPanel>("menu");
  const runCi = useRunCiAction(repo, pr.number, plan, canPush);
  const state = {
    isOpen: pr.state === "OPEN",
    hasReviewFindings: !!latestReview,
    failedCount: failed.length,
    ciState: plan.state,
  };
  const active = activePrActionPanel(panel, state);
  const items = prMenuItems(state);
  const mergeLabel = pr.mergeable === "CONFLICTING" ? `${t("Resolve conflicts with")} ${pr.base_ref}` : `${t("Merge")} ${pr.base_ref}`;

  if (active !== "menu") {
    const titles: Record<Exclude<PrActionPanel, "menu">, string> = {
      fix: t("Fix with agent"),
      reproduce: t("Reproduce"),
      "fix-ci": t("Fix CI"),
      "merge-base": mergeLabel,
    };
    return (
      <div>
        <div className="pop-back">
          <button type="button" className="ghost sm" aria-label={t("Back")} onClick={() => setPanel("menu")}>
            ← {t("Actions")}
          </button>
          <span className="small muted truncate">{titles[active]}</span>
        </div>
        {active === "fix" && latestReview && <FixForm repo={repo} number={pr.number} review={latestReview} onClose={onClose} />}
        {active === "reproduce" && <ReproduceForm repo={repo} number={pr.number} onClose={onClose} />}
        {active === "fix-ci" && <FixCiForm repo={repo} number={pr.number} failed={failed} canPush={canPush} onClose={onClose} />}
        {active === "merge-base" && (
          <MergeBaseForm
            repo={repo}
            number={pr.number}
            base={pr.base_ref}
            conflicting={pr.mergeable === "CONFLICTING"}
            canPush={canPush}
            onClose={onClose}
          />
        )}
      </div>
    );
  }

  return (
    <>
      {items.includes("fix") && (
        <MenuItem icon="sparkles" onClick={() => setPanel("fix")}>
          {t("Fix with agent")}
        </MenuItem>
      )}
      {items.includes("reproduce") && (
        <MenuItem icon="server" onClick={() => setPanel("reproduce")}>
          {t("Reproduce")}
        </MenuItem>
      )}
      {items.includes("fix-ci") && (
        <MenuItem icon="activity" hint={failed.length} onClick={() => setPanel("fix-ci")}>
          {t("Fix CI")}
        </MenuItem>
      )}
      {items.includes("run-ci") && (
        <MenuItem
          icon={plan.state === "approval" ? "shield" : "play"}
          disabled={runCi.disabled}
          title={runCi.title}
          onClick={() => runCi.trigger(onClose)}
        >
          {runCi.label}
        </MenuItem>
      )}
      {items.includes("merge-base") && (
        <MenuItem icon="merge" onClick={() => setPanel("merge-base")}>
          {mergeLabel}
        </MenuItem>
      )}
      {items.length > 1 && <div className="menu-sep" />}
      <MenuItem icon="external" href={pr.url} onClick={onClose}>
        {openOnForge}
      </MenuItem>
    </>
  );
}

function useRunCiAction(repo: string, number: number, plan: RunCiPlan, canPush: boolean) {
  const t = useT();
  const toast = useToast();
  const run = useAct(
    () => api.post<{ actions: { kind: string; status: string; error: string | null }[] }>(`/api/prs/${repo}/${number}/ci`),
    [["pr"], ["prs"], ["audit"]],
  );
  const display = describeRunCi(plan.state, canPush);
  const held = (plan.waiting ?? []).join(", ");
  const what =
    plan.state === "gated"
      ? `${t("Adds the label")} ${plan.label}, ${t("which starts the full CI")}`
      : plan.state === "failed"
        ? `${t("Reruns the failed jobs of")} ${plan.reruns.map((r) => r.workflow || `#${r.run_id}`).join(", ")}`
        : "";
  const title =
    plan.state === "approval"
      ? canPush
        ? `${t("GitHub holds these until a maintainer approves running them:")} ${held}. ${t("Approves them as you; read the PR's changes to its workflows first.")}`
        : t("Sign in with GitHub, so the approval comes from you.")
      : what;
  const label =
    plan.state === "approval"
      ? run.isPending
        ? t("Approving…")
        : t("Approve and run CI")
      : run.isPending
        ? t("Starting…")
        : t("Run CI");
  const trigger = (onDone?: () => void) =>
    run.mutate(undefined, {
      onSuccess: (out) => {
        onDone?.();
        const blocked = out.actions.filter((a) => !["executed", "queued", "intent"].includes(a.status));
        if (plan.state === "approval") {
          toast(
            blocked.length
              ? { title: t("Not approved"), body: blocked.map((a) => a.error || a.status).join("; "), icon: "pause" }
              : { title: t("CI approved"), body: `${t("GitHub runs")} ${held}.`, icon: "play" },
          );
        } else {
          toast(
            blocked.length
              ? { title: t("Not run"), body: blocked.map((a) => a.error || a.status).join("; "), icon: "pause" }
              : { title: plan.state === "gated" ? t("CI started") : t("Rerunning failed CI"), body: what, icon: "play" },
          );
        }
      },
      onError: (e) =>
        toast({
          title: plan.state === "approval" ? t("Not approved") : t("Not run"),
          body: String((e as Error).message),
          icon: "x",
        }),
    });
  return { display, disabled: display.disabled || run.isPending, title, label, trigger };
}

/** Get CI going on the PR's head: add the CI gate label, or rerun what failed. The
 * click is the approval; it runs at once unless the policy holds it. */
function RunCiButton({
  repo,
  number,
  plan,
  canPush,
  size,
}: {
  repo: string;
  number: number;
  plan: RunCiPlan;
  canPush: boolean;
  size?: "sm";
}) {
  const t = useT();
  const ci = useRunCiAction(repo, number, plan, canPush);
  if (ci.display.inline === "none") return null;
  if (ci.display.inline === "pill") {
    return (
      <span className="pill status-ok" title={t("CI passed at this commit; there is nothing to run")}>
        <Icon name="check" size={12} />
        {t("CI passed")}
      </span>
    );
  }
  if (ci.display.inline === "running") {
    return (
      <button className={size} disabled title={t("CI is running at this commit")}>
        <span className="pulse" /> {t("CI running")}
      </button>
    );
  }
  return (
    <button className={size} disabled={ci.disabled} title={ci.title} onClick={() => ci.trigger()}>
      <Icon name={plan.state === "approval" ? "shield" : "play"} size={14} />
      {ci.label}
    </button>
  );
}

/** Where this PR stands among the repository's armed PRs: they take turns so a merge
 * does not make the others run their CI twice. */
function MergeQueueLine({ q, number, repo }: { q: NonNullable<PrDetail["merge_queue"]>; number: number; repo: string }) {
  const t = useT();
  const position = q.order.findIndex((e) => e.number === number);
  return (
    <div className="merge-queue small">
      <div>
      <span className="muted">{t("Merge queue")}:</span>{" "}
      {q.order.map((e, i) => (
        <span key={e.number}>
          {i > 0 && " → "}
          {e.number === number ? (
            <strong>
              #{e.number} {t("(this PR)")}
            </strong>
          ) : (
            <Link to={prPath(repo, e.number)} title={e.title}>
              #{e.number}
            </Link>
          )}
          {e.number === q.head && <span className="pill status-info">{t("its turn")}</span>}
        </span>
      ))}
      </div>
      {position > 0 && q.head !== number && (
        <div className="muted">{t("It merges main and runs its CI when its turn comes, so the PRs before it do not make it run CI twice.")}</div>
      )}
    </div>
  );
}
