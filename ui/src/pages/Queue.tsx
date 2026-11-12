// Pull requests: every open PR the user can see, ranked by internal priority, with saved
// views, search, filters, sorting, grouping, and review in bulk. The view lives in the URL.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Me, type PrRow, type ReviewActivity, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { ErrorBox, Labels, PageHead, prPath, Quality, QUALITY_BANDS, qualityLabel, ReviewState, shownQuality, showsQuick, Tier, timeOf, useAct, When } from "../components";
import { evaluationToast, priorityRank, prKey, qualityToast, REVIEW_ORDER, REVIEW_STATES, TIER_ORDER, usePrs, useReevaluate, useRepos, useScoreQuality, useSyncPrs } from "../data";
import { RepoTabs, useRepoScope } from "../repoScope";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { agentName, AgentPicker, reviewToast, useAgentChoice } from "../agentPicker";
import { openJob } from "../openJob";
import { ScoringProgress } from "../jobs";
import { BulkBar, DataTable, ListToolbar, useList, type Column } from "../list";
import { MenuItem, Popover, Skeleton, Tabs, useRememberedView, useTab, useToast } from "../ui";
import { holds, Person, PrPeopleLine } from "../avatar";
import { useAliasOf } from "../aliases";
import { ProgressInline } from "../progress";

const NEEDS_REVIEW = new Set(["NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS"]);

export default function QueuePage({ me }: { me: Me }) {
  const t = useT();
  const navigate = useNavigate();
  const toast = useToast();
  const agent = useAgent();
  const all = usePrs();
  const aliasOf = useAliasOf();
  // One repository at a time: each ranks its PRs by its own rubric.
  const repos = useRepos();
  const names = (repos.data ?? []).map((r) => r.repo).sort((a, b) => a.localeCompare(b));
  const [scope, setScope] = useRepoScope(names);
  const viewParams = useRememberedView("prs", scope);
  const allRows = all.data ?? [];
  const rows = allRows.filter((p) => !scope || p.repo === scope);
  const myRows = rows.filter((p) => holds(p.assignees, me.login, "owner"));
  const reviewingRows = rows.filter((p) => holds(p.assignees, me.login, "reviewer"));
  const views = [
    { id: "all", label: t("All open"), count: rows.length },
    { id: "review", label: t("Needs review"), count: rows.filter((p) => NEEDS_REVIEW.has(p.review_state)).length },
    { id: "mine", label: t("Owned by me"), count: myRows.length },
    { id: "reviewing", label: t("I review"), count: reviewingRows.length },
    { id: "unrated", label: t("Not evaluated"), count: rows.filter((p) => !p.tier).length },
    { id: "reviewed", label: t("Reviewed"), count: rows.filter((p) => p.review_state === "UP_TO_DATE").length },
  ];
  const [view, setView] = useTab(views, "tab", viewParams);
  const source =
    view === "mine"
      ? myRows
      : view === "reviewing"
        ? reviewingRows
        : view === "review"
        ? rows.filter((p) => NEEDS_REVIEW.has(p.review_state))
        : view === "unrated"
          ? rows.filter((p) => !p.tier)
          : view === "reviewed"
            ? rows.filter((p) => p.review_state === "UP_TO_DATE")
            : rows;

  const bandOf = (p: PrRow) => {
    const shown = shownQuality(p.quality, p.quick_quality, p.head);
    return shown ? t(qualityLabel(shown.band)) : t("Not scored yet");
  };

  const list = useList<PrRow>({
    rows: source,
    key: prKey,
    search: (p) => `${p.title} ${p.repo}#${p.number} #${p.number} ${p.labels.join(" ")} ${p.summary} ${[p.author, ...p.assignees.map((a) => a.login)].map((l) => `${l} ${aliasOf(l) ?? ""}`).join(" ")}`,
    facets: [
      { id: "tier", label: t("Priority"), values: (p) => p.tier ?? t("Not evaluated") },
      { id: "state", label: t("Review state"), values: (p) => t(REVIEW_STATES[p.review_state] ?? p.review_state) },
      { id: "quality", label: t("Quality"), values: (p) => bandOf(p) },
      { id: "scored", label: t("Quality from"), values: (p) => (showsQuick(p.quality, p.quick_quality, p.head) ? t("Quick score") : p.quality ? t("Review") : t("Not scored yet")) },
      { id: "author", label: t("Author"), values: (p) => p.author },
      { id: "label", label: t("Label"), values: (p) => p.labels },
      { id: "has_owner", label: t("Has an owner"), values: (p) => (namesOf(p, "owner") ? t("Yes") : t("No")) },
      { id: "has_reviewers", label: t("Has reviewers"), values: (p) => (namesOf(p, "reviewer") ? t("Yes") : t("No")) },
      { id: "owners", label: t("Owners"), values: (p) => namesOf(p, "owner") ?? t("Nobody") },
      { id: "claimed", label: t("Reviewers"), values: (p) => namesOf(p, "reviewer") ?? t("Nobody") },
    ],
    sorts: [
      { id: "priority", label: t("Priority"), value: (p) => priorityRank(p), descFirst: true },
      { id: "tier", label: t("Priority"), value: (p) => (p.tier ? TIER_ORDER.indexOf(p.tier) : null) },
      { id: "quality", label: t("Quality"), value: (p) => shownQuality(p.quality, p.quick_quality, p.head)?.score, descFirst: true },
      { id: "opened", label: t("Opened"), value: (p) => timeOf(p.created_at), descFirst: true },
      { id: "addressed", label: t("Feedback addressed"), value: (p) => p.progress?.addressed ?? null, descFirst: true },
      { id: "updated", label: t("Last updated"), value: (p) => timeOf(p.updated_at), descFirst: true },
      { id: "number", label: t("Number"), value: (p) => p.number, descFirst: true },
      { id: "title", label: t("Title"), value: (p) => p.title.toLowerCase() },
      { id: "author", label: t("Author"), value: (p) => p.author.toLowerCase() },
      { id: "state", label: t("Review state"), value: (p) => REVIEW_ORDER.indexOf(p.review_state) },
    ],
    defaultSort: "-priority",
    groups: [
      { id: "tier", label: t("Priority"), value: (p) => p.tier ?? t("Not evaluated"), order: [...TIER_ORDER, t("Not evaluated")] },
      { id: "state", label: t("Review state"), value: (p) => t(REVIEW_STATES[p.review_state] ?? p.review_state), order: REVIEW_ORDER.map((s) => t(REVIEW_STATES[s])) },
      {
        id: "quality",
        label: t("Quality"),
        value: bandOf,
        order: [...QUALITY_BANDS.map((b) => t(b.label)), t("Not scored yet")],
      },
      { id: "author", label: t("Author"), value: (p) => p.author },
      { id: "owners", label: t("Owners"), value: (p) => namesOf(p, "owner")?.join(", ") ?? t("Nobody") },
      { id: "claimed", label: t("Reviewers"), value: (p) => namesOf(p, "reviewer")?.join(", ") ?? t("Nobody") },
    ],
    pageSize: 50,
    searchParams: viewParams,
  });

  const openPr = (p: PrRow) => navigate(prPath(p.repo, p.number), { state: { from: "prs" } });

  const [reviewer, setReviewer] = useAgentChoice("review");
  const again = useReevaluate();
  const quality = useScoreQuality();
  const sync = useSyncPrs();
  const [confirmAll, setConfirmAll] = useState<"priority" | "quality" | null>(null);
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
    {
      id: "tier",
      header: t("Priority"),
      sort: "priority",
      width: 70,
      cell: (p) => (
        <span className="stack" style={{ gap: 2, alignItems: "flex-start" }}>
          <Tier tier={p.tier} />
          {p.total != null && (
            <span className="small muted num" title={`${t("Priority")} ${p.total.toFixed(1)}`} aria-label={`${t("Priority")} ${p.total.toFixed(1)}`}>
              {p.total.toFixed(1)}
            </span>
          )}
        </span>
      ),
    },
    {
      id: "quality",
      header: t("Quality"),
      cell: (p) => <Quality quality={p.quality} quick={p.quick_quality} head={p.head} compact />,
      sort: "quality",
      width: 72,
    },
    {
      id: "title",
      primary: true,
      header: t("Pull request"),
      sort: "title",
      cell: (p) => (
        <div className="title-cell">
          <span className="title-line">
            <Link to={prPath(p.repo, p.number)} state={{ from: "prs" }} style={{ color: "var(--text)", fontWeight: 550 }}>
              {p.title}
            </Link>
            {p.labels.length > 0 && <Labels labels={p.labels} max={2} />}
          </span>
          <div className="small muted">
            #{p.number} · <Person login={p.author} />
            {p.draft ? ` · ${t("draft")}` : ""}
          </div>
          {p.assignees.length > 0 && (
            <div className="small">
              <PrPeopleLine people={p.assignees} me={me.login} t={t} />
            </div>
          )}
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
          <ProgressInline p={p.progress} />
          <Activity a={p.review_activity} me={me.login} />
        </div>
      ),
    },
    {
      id: "updated",
      header: t("Activity"),
      sort: "updated",
      className: "small hide-narrow",
      cell: (p) => (
        <span className="stack" style={{ gap: 2, whiteSpace: "nowrap" }}>
          <span>
            {t("updated")} <When at={p.updated_at} />
          </span>
          <span className="muted">
            {t("opened")} <When at={p.created_at} />
          </span>
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      width: 84,
      className: "hide-phone",
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
                <MenuItem icon="eye" onClick={() => (openPr(p), close())}>
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
  const inView = list.all;
  const scoreQuality = (prs: PrRow[], done?: () => void) =>
    quality.mutate(
      { prs: prs.map((p) => ({ repo: p.repo, number: p.number })) },
      {
        onSuccess: (out) => {
          done?.();
          toast(qualityToast(out, t));
        },
      },
    );
  const scoreAgain = (prs: PrRow[], done?: () => void) =>
    again.mutate(
      prs.map((p) => ({ repo: p.repo, number: p.number })),
      {
        onSuccess: (out) => {
          done?.();
          toast(evaluationToast(out, t));
        },
      },
    );
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
          <>
            <button
              disabled={sync.isPending}
              title={t("Read every repository's PRs from GitHub now instead of at the next poll")}
              onClick={() =>
                sync.mutate(scope ? [scope] : [], {
                  onSuccess: (r) =>
                    toast({
                      title: t("Checking GitHub for changes"),
                      body: `${r.started.length + r.running.length} ${t("repositories")} · ${t("the list updates as each finishes")}`,
                      icon: "refresh",
                    }),
                })
              }
            >
              <Icon name="refresh" />
              {t("Sync with GitHub")}
            </button>
            {confirmAll ? (
              <>
                <button
                  className="primary"
                  disabled={again.isPending || quality.isPending}
                  onClick={() =>
                    confirmAll === "priority"
                      ? scoreAgain(inView, () => setConfirmAll(null))
                      : scoreQuality(inView, () => setConfirmAll(null))
                  }
                >
                  {again.isPending || quality.isPending
                    ? t("Starting…")
                    : `${confirmAll === "priority" ? t("Score priority") : t("Score quality")}: ${inView.length} PR`}
                </button>
                <button className="ghost" onClick={() => setConfirmAll(null)}>
                  {t("Cancel")}
                </button>
              </>
            ) : (
              <>
                <button
                  disabled={!inView.length}
                  title={t("An agent scores every PR in this view again, with the repository's lessons as they are now; each PR is one agent run")}
                  onClick={() => setConfirmAll("priority")}
                >
                  <Icon name="sparkles" />
                  {t("Score priority")}
                </button>
                <button
                  disabled={!inView.length}
                  title={t("An agent scores the quality of every PR in this view: a reviewed PR from its review, the rest quickly from their diffs in the background; each PR is one agent run")}
                  onClick={() => setConfirmAll("quality")}
                >
                  <Icon name="sparkles" />
                  {t("Score quality")}
                </button>
              </>
            )}
            <button className="agent" onClick={() => agent.open()}>
              <Icon name="sparkles" />
              {t("Ask about the queue")}
            </button>
          </>
        }
      />
      <RepoTabs names={names} scope={scope} onPick={setScope} count={(repo) => allRows.filter((p) => p.repo === repo).length} />
      <ScoringProgress key={scope} repo={scope} me={me.login} />
      <Tabs tabs={views} active={view} onChange={setView} />
      <ListToolbar list={list} placeholder={t("Search by title, number, author, label…")} />
      <ErrorBox error={all.error || review.error || again.error || quality.error || sync.error} />
      {all.isLoading ? (
        <div className="card">
          <Skeleton rows={8} />
        </div>
      ) : (
        <DataTable
          list={list}
          columns={columns}
          onOpen={openPr}
          selectable={(p) => p.review_state !== "UP_TO_DATE"}
          empty={view === "review" ? t("No PR needs a review.") : t("No open PRs.")}
        />
      )}
      <BulkBar count={selected.length} onClear={list.clearSelection}>
        <AgentPicker value={reviewer} onChange={setReviewer} compact />
        <button className="primary sm" disabled={review.isPending} onClick={() => startReview(selected)}>
          {review.isPending ? t("Starting…") : `${t("Review")} ${selected.length}`}
        </button>
        <button className="sm" disabled={again.isPending} onClick={() => scoreAgain(selected, list.clearSelection)}>
          <Icon name="sparkles" size={14} />
          {`${t("Score priority")} ${selected.length}`}
        </button>
        <button
          className="sm"
          disabled={quality.isPending}
          title={t("An agent scores each PR by the repository's quality guide: a reviewed PR from its review, any other quickly from its diff")}
          onClick={() => scoreQuality(selected, list.clearSelection)}
        >
          <Icon name="sparkles" size={14} />
          {`${t("Score quality")} ${selected.length}`}
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

/** The logins holding ``role`` on a PR; ``null`` when nobody does. */
function namesOf(p: PrRow, role: "owner" | "reviewer"): string[] | null {
  const names = p.assignees.filter((a) => a.role === role).map((a) => a.login);
  return names.length ? names : null;
}
