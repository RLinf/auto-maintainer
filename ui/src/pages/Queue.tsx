// Pull requests: every open PR the user can see, ranked by internal priority, with saved
// views, search, filters, sorting, grouping, and review in bulk. The view lives in the URL.

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, type Me, type PrRow, type ReviewActivity, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { ErrorBox, Labels, PageHead, prPath, ReviewState, Tier, timeOf, useAct, When } from "../components";
import { prKey, REVIEW_ORDER, REVIEW_STATES, TIER_ORDER, usePrs } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { agentName, AgentPicker, reviewToast, useAgentChoice } from "../agentPicker";
import { openJob } from "../openJob";
import { BulkBar, DataTable, ListToolbar, useList, type Column } from "../list";
import { MenuItem, Popover, Skeleton, Tabs, useTab, useToast } from "../ui";

const NEEDS_REVIEW = new Set(["NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS"]);

export default function QueuePage({ me }: { me: Me }) {
  const t = useT();
  const navigate = useNavigate();
  const toast = useToast();
  const agent = useAgent();
  const all = usePrs();
  const mine = useQuery({ queryKey: ["prs", "mine"], queryFn: () => api.get<PrRow[]>("/api/prs?mine=true") });
  const rows = all.data ?? [];
  const views = [
    { id: "all", label: t("All open"), count: rows.length },
    { id: "review", label: t("Needs review"), count: rows.filter((p) => NEEDS_REVIEW.has(p.review_state)).length },
    { id: "mine", label: t("Mine"), count: mine.data?.length },
    { id: "unrated", label: t("Not evaluated"), count: rows.filter((p) => !p.tier).length },
    { id: "reviewed", label: t("Reviewed"), count: rows.filter((p) => p.review_state === "UP_TO_DATE").length },
  ];
  const [view, setView] = useTab(views);
  const source =
    view === "mine"
      ? mine.data ?? []
      : view === "review"
        ? rows.filter((p) => NEEDS_REVIEW.has(p.review_state))
        : view === "unrated"
          ? rows.filter((p) => !p.tier)
          : view === "reviewed"
            ? rows.filter((p) => p.review_state === "UP_TO_DATE")
            : rows;

  const list = useList<PrRow>({
    rows: source,
    key: prKey,
    search: (p) => `${p.title} ${p.repo}#${p.number} #${p.number} ${p.author} ${p.labels.join(" ")} ${p.summary} ${p.claimed_by ?? ""}`,
    facets: [
      { id: "repo", label: t("Repository"), values: (p) => p.repo },
      { id: "tier", label: t("Priority"), values: (p) => p.tier ?? t("Not evaluated") },
      { id: "state", label: t("Review state"), values: (p) => t(REVIEW_STATES[p.review_state] ?? p.review_state) },
      { id: "author", label: t("Author"), values: (p) => p.author },
      { id: "label", label: t("Label"), values: (p) => p.labels },
      { id: "claimed", label: t("Claimed by"), values: (p) => p.claimed_by ?? t("Unclaimed") },
    ],
    sorts: [
      { id: "priority", label: t("Priority"), value: (p) => p.total ?? -1, descFirst: true },
      { id: "tier", label: t("Priority"), value: (p) => (p.tier ? TIER_ORDER.indexOf(p.tier) : 99) },
      { id: "updated", label: t("Last updated"), value: (p) => timeOf(p.updated_at), descFirst: true },
      { id: "number", label: t("Number"), value: (p) => p.number, descFirst: true },
      { id: "title", label: t("Title"), value: (p) => p.title.toLowerCase() },
      { id: "author", label: t("Author"), value: (p) => p.author.toLowerCase() },
      { id: "state", label: t("Review state"), value: (p) => REVIEW_ORDER.indexOf(p.review_state) },
    ],
    defaultSort: "-priority",
    groups: [
      { id: "repo", label: t("Repository"), value: (p) => p.repo },
      { id: "tier", label: t("Priority"), value: (p) => p.tier ?? t("Not evaluated"), order: [...TIER_ORDER, t("Not evaluated")] },
      { id: "state", label: t("Review state"), value: (p) => t(REVIEW_STATES[p.review_state] ?? p.review_state), order: REVIEW_ORDER.map((s) => t(REVIEW_STATES[s])) },
      { id: "author", label: t("Author"), value: (p) => p.author },
      { id: "claimed", label: t("Claimed by"), value: (p) => p.claimed_by ?? t("Unclaimed") },
    ],
    pageSize: 50,
  });

  const [reviewer, setReviewer] = useAgentChoice("review");
  const review = useAct(
    (prs: PrRow[]) => api.post<ReviewStart[]>("/api/reviews", { prs: prs.map((r) => ({ repo: r.repo, number: r.number })), agent: reviewer || null }),
    [["prs"], ["proposals"]],
  );
  const startReview = (prs: PrRow[]) =>
    review.mutate(prs, {
      onSuccess: (out) => {
        list.clearSelection();
        toast(reviewToast(out, t, () => navigate("/inbox")));
      },
    });

  const askAbout = (p: PrRow) => agent.open({ context: { page: "pr", repo: p.repo, number: p.number, label: `${p.repo}#${p.number}` } });

  const columns: Column<PrRow>[] = [
    { id: "tier", header: t("Priority"), cell: (p) => <Tier tier={p.tier} />, sort: "tier", width: 64 },
    {
      id: "score",
      header: t("Score"),
      cell: (p) => (p.total == null ? <span className="muted">—</span> : p.total.toFixed(1)),
      sort: "priority",
      className: "num",
      width: 72,
    },
    {
      id: "title",
      header: t("Pull request"),
      sort: "title",
      cell: (p) => (
        <div style={{ minWidth: 260 }}>
          <Link to={prPath(p.repo, p.number)} style={{ color: "var(--text)", fontWeight: 550 }}>
            {p.title}
          </Link>
          <div className="small muted">
            {p.repo}#{p.number} · {p.author} · <When at={p.updated_at} />
            {p.draft ? ` · ${t("draft")}` : ""}
          </div>
          {p.summary && (
            <div className="small muted truncate hide-narrow" style={{ maxWidth: 560 }} title={p.summary}>
              {p.summary}
            </div>
          )}
        </div>
      ),
    },
    {
      id: "state",
      header: t("Review"),
      sort: "state",
      cell: (p) => (
        <div className="stack" style={{ gap: 4, alignItems: "flex-start" }}>
          <ReviewState state={p.review_state} />
          <Activity a={p.review_activity} me={me.login} />
        </div>
      ),
    },
    { id: "labels", header: t("Labels"), cell: (p) => <Labels labels={p.labels} max={3} />, className: "hide-narrow" },
    {
      id: "claimed",
      header: t("Claimed by"),
      cell: (p) => (p.claimed_by ? (p.claimed_by === me.login ? <strong>{t("you")}</strong> : p.claimed_by) : <span className="muted">—</span>),
      className: "hide-narrow",
    },
    {
      id: "actions",
      header: "",
      width: 84,
      cell: (p) => (
        <span className="row" style={{ gap: 2, justifyContent: "flex-end", flexWrap: "nowrap" }}>
          <button className="ghost icon sm" title={t("Ask agent")} aria-label={t("Ask agent")} onClick={() => askAbout(p)}>
            <Icon name="sparkles" size={14} />
          </button>
          <Popover
            align="right"
            label={t("Actions")}
            trigger={(_, toggle) => (
              <button className="ghost icon sm" aria-label={t("More actions")} onClick={toggle}>
                <Icon name="more" size={14} />
              </button>
            )}
          >
            {(close) => (
              <>
                <MenuItem icon="eye" onClick={() => (navigate(prPath(p.repo, p.number)), close())}>
                  {t("Open")}
                </MenuItem>
                <MenuItem icon="pr" onClick={() => (startReview([p]), close())}>
                  {p.reviewed_sha ? t("Follow up") : t("Review")}
                </MenuItem>
                <MenuItem icon="sparkles" onClick={() => (askAbout(p), close())}>
                  {t("Ask agent")}
                </MenuItem>
                <MenuItem icon="external" href={p.url} onClick={close}>
                  {t("Open on GitHub")}
                </MenuItem>
              </>
            )}
          </Popover>
        </span>
      ),
    },
  ];

  const selected = list.selected;
  return (
    <>
      <PageHead
        title={t("Pull requests")}
        sub={
          <span className="row" style={{ gap: 6 }}>
            <Icon name="shield" size={14} />
            {t("Tiers and scores are internal: they are never posted.")}
          </span>
        }
        actions={
          <button className="agent" onClick={() => agent.open()}>
            <Icon name="sparkles" />
            {t("Ask about the queue")}
          </button>
        }
      />
      <Tabs tabs={views} active={view} onChange={setView} />
      <ListToolbar list={list} placeholder={t("Search by title, number, author, label…")} />
      <ErrorBox error={all.error || review.error} />
      {all.isLoading || (view === "mine" && mine.isLoading) ? (
        <div className="card">
          <Skeleton rows={8} />
        </div>
      ) : (
        <DataTable
          list={list}
          columns={columns}
          onOpen={(p) => navigate(prPath(p.repo, p.number))}
          selectable={(p) => p.review_state !== "UP_TO_DATE"}
          empty={view === "review" ? t("No PR needs a review.") : t("No open PRs.")}
        />
      )}
      <BulkBar count={selected.length} onClear={list.clearSelection}>
        <AgentPicker value={reviewer} onChange={setReviewer} compact />
        <button className="primary sm" disabled={review.isPending} onClick={() => startReview(selected)}>
          {review.isPending ? t("Starting…") : `${t("Review")} ${selected.length}`}
        </button>
        <button
          className="sm"
          onClick={() =>
            agent.open({
              prompt: `${t("Compare these PRs and tell me which to review first")}: ${selected.map((p) => `${p.repo}#${p.number}`).join(", ")}`,
              send: true,
            })
          }
        >
          <Icon name="sparkles" size={14} />
          {t("Compare")}
        </button>
      </BulkBar>
    </>
  );
}

function Activity({ a, me }: { a: ReviewActivity; me: string }) {
  const t = useT();
  const job = a.job;
  if (job && (job.status === "queued" || job.status === "running" || job.status === "waiting")) {
    return (
      <button
        className="link-button small"
        style={{ color: "var(--info)" }}
        title={t("Watch the agent work")}
        onClick={(e) => {
          e.stopPropagation();
          openJob(job.id);
        }}
      >
        <span className="pulse" /> {job.playbook === "followup" ? t("following up…") : t("reviewing…")}
        {job.agent ? ` ${agentName(job.agent)}` : ""}
        {job.by ? ` · ${job.by === me ? t("you") : job.by}` : ""}
      </button>
    );
  }
  if (a.draft) {
    return (
      <Link to="/inbox" className="small">
        {!a.draft.for ? t("draft") : a.draft.for === me ? t("draft for you") : `${t("draft for")} ${a.draft.for}`} →
      </Link>
    );
  }
  if (job && job.status === "failed") {
    return (
      <span className="small" style={{ color: "var(--bad)" }} title={job.error ?? ""}>
        <Icon name="x" size={12} /> {t("review failed")}
      </span>
    );
  }
  if (a.posted) {
    return (
      <span className="small muted">
        {t("posted at")} <code>{a.posted.sha.slice(0, 7)}</code>
      </span>
    );
  }
  return null;
}
