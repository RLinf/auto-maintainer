// Home: the control center, in one column. A status line; for newcomers, the steps to
// get going; what agents are doing for the user right now; the PRs to review with one
// click (and when reviews run on their own); and every decision waiting on the user,
// answerable in place.

import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type Me, type ServiceView } from "../api";
import { CaughtUp, ErrorBox, Tier, timeOf, useAct } from "../components";
import { useMemoryChanges, useProposals, usePrs, useQuestions, useRepos, useService } from "../data";
import { useT } from "../i18n";
import { RepoTabs, useRepoScope } from "../repoScope";
import { Icon } from "../icons";
import { ProposalCard } from "../proposal";
import { Skeleton, useTab } from "../ui";
import { ACTIVE, useWork } from "../jobs";
import { AgentWork, GettingStarted, ReviewCard, useAutomations } from "./homeParts";
import { ChangeCard } from "./Memory";
import { QuestionCard } from "./Questions";

const NEEDS_REVIEW = new Set(["NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS"]);

type Kind = "question" | "proposal" | "memory" | "update";

interface FeedItem {
  id: string;
  kind: Kind;
  at: string | null;
  node: ReactNode;
  mine?: boolean;
}

const KIND_LABEL: Record<Kind, string> = {
  question: "Agent question",
  proposal: "Approval",
  memory: "Memory change",
  update: "Service update",
};

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export default function HomePage({ me }: { me: Me }) {
  const t = useT();
  const prs = usePrs();
  const questions = useQuestions("pending");
  const proposals = useProposals();
  const memory = useMemoryChanges();
  const repos = useRepos();
  const service = useService();

  // One repository at a time: priorities come from each repository's own rubric, so
  // PRs, counts, and tiers are never mixed across repositories.
  const repoRows = repos.data ?? [];
  const names = repoRows.map((r) => r.repo).sort((a, b) => a.localeCompare(b));
  const [scope, setScope] = useRepoScope(names);
  const inScope = (repo: string | null | undefined) => !scope || !repo || repo === scope;
  const allRows = prs.data ?? [];
  const rows = allRows.filter((p) => !scope || p.repo === scope);
  const needs = rows.filter((p) => NEEDS_REVIEW.has(p.review_state));
  const byTier = (tier: string) => rows.filter((p) => p.tier === tier).length;
  const repoQuery = scope ? `&repo=${encodeURIComponent(scope)}` : "";
  const sv = service.data;
  const updates = serviceUpdates(sv);
  const automations = useAutomations(repoRows);
  const work = useWork(false);

  const items = useMemo<FeedItem[]>(() => {
    const out: FeedItem[] = [];
    for (const q of (questions.data ?? []).filter((x) => inScope(x.repo))) out.push({ id: q.id, kind: "question", at: q.created_at, node: <QuestionCard question={q} compact />, mine: q.asked_for === me.login });
    for (const p of (proposals.data ?? []).filter((x) => inScope(x.repo))) out.push({ id: p.id, kind: "proposal", at: p.created_at, node: <ProposalCard proposal={p} compact /> });
    for (const c of (memory.data ?? []).filter((x) => !scope || !x.repos.length || x.repos.includes(scope))) out.push({ id: c.id, kind: "memory", at: c.created_at, node: <ChangeCard change={c} compact /> });
    for (const u of updates) out.push(u);
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questions.data, proposals.data, memory.data, updates, me.login, scope]);

  const counts: Record<Kind, number> = { question: 0, proposal: 0, memory: 0, update: 0 };
  for (const i of items) counts[i.kind] += 1;
  const tabs = [
    { id: "all", label: t("All"), count: items.length },
    { id: "question", label: t("Questions"), count: counts.question },
    { id: "proposal", label: t("Approvals"), count: counts.proposal },
    { id: "memory", label: t("Memory"), count: counts.memory },
    { id: "update", label: t("Updates"), count: counts.update },
  ];
  const [show, setShow] = useTab(tabs, "show");
  const [order, setOrder] = useTab(
    [
      { id: "new", label: t("Newest first") },
      { id: "old", label: t("Oldest first") },
    ],
    "order",
  );
  // Questions block a running agent, so they lead; then by time.
  const feed = items
    .filter((i) => show === "all" || i.kind === show)
    .sort((a, b) => {
      if (!!a.mine !== !!b.mine) return a.mine ? -1 : 1;
      if ((a.kind === "question") !== (b.kind === "question")) return a.kind === "question" ? -1 : 1;
      const d = (timeOf(b.at) ?? 0) - (timeOf(a.at) ?? 0);
      return order === "old" ? -d : d;
    });
  const loadingFeed = questions.isLoading || proposals.isLoading || memory.isLoading;
  const first = (me.name || me.login).split(" ")[0];

  const running = (work.data ?? []).filter((j) => ACTIVE.has(j.status)).length;
  const p0 = byTier("P0");
  const p1 = byTier("P1");
  return (
    <div className="home">
      <div className="hero">
        <div className="titles">
          <h1>
            {t(greeting())}, {first}
          </h1>
          <p className="status-line">
            <a href="#decisions">
              <Icon name="inbox" size={14} />
              {items.length ? `${items.length} ${t(items.length === 1 ? "decision waits for you" : "decisions wait for you")}` : t("Nothing waits for your decision.")}
            </a>
            <Link to={`/prs?tab=review${repoQuery}`}>
              <Icon name="pr" size={14} />
              {prs.isLoading ? "…" : needs.length} {t("PRs need review")}
            </Link>
            {p0 > 0 && (
              <Link to={`/prs?f.tier=P0${repoQuery}`} title={t("Open PRs at P0")}>
                <Tier tier="P0" /> {p0}
              </Link>
            )}
            {p1 > 0 && (
              <Link to={`/prs?f.tier=P1${repoQuery}`} title={t("Open PRs at P1")}>
                <Tier tier="P1" /> {p1}
              </Link>
            )}
            {running > 0 && (
              <a href="#work">
                <span className="pulse" /> {running} {t(running === 1 ? "agent working" : "agents working")}
              </a>
            )}
            <Link to="/service">
              <Icon name={me.paused ? "pause" : "server"} size={14} />
              {me.paused ? t("Paused") : t("Running")}
              {sv?.status?.pending.length ? ` · ${sv.status.pending.length} ${t("updates to deploy")}` : ""}
            </Link>
          </p>
        </div>
      </div>

      <RepoTabs names={names} scope={scope} onPick={setScope} count={(repo) => allRows.filter((p) => p.repo === repo && NEEDS_REVIEW.has(p.review_state)).length} />
      <GettingStarted repos={repoRows} prs={rows} automations={automations} />
      <AgentWork me={me} />
      <ReviewCard rows={needs} loading={prs.isLoading} me={me} automations={automations} />

      <section id="decisions" className="stack" style={{ gap: 12, minWidth: 0 }}>
        <div className="row">
          <h2 style={{ fontSize: 17 }}>{t("Needs your decision")}</h2>
          <span className="spacer" />
          <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
            {tabs.map((tab) => (
              <button key={tab.id} className={`chip-btn ${show === tab.id ? "on" : ""}`} style={{ borderStyle: "solid", height: 30 }} onClick={() => setShow(tab.id)}>
                {tab.label}
                {tab.count ? <span className="muted">{tab.count}</span> : null}
              </button>
            ))}
          </div>
          <button className="ghost sm" onClick={() => setOrder(order === "new" ? "old" : "new")} title={t("Sort")}>
            <Icon name={order === "new" ? "sortDesc" : "sortAsc"} size={14} />
            {order === "new" ? t("Newest first") : t("Oldest first")}
          </button>
        </div>
        <ErrorBox error={questions.error || proposals.error || memory.error} />
        {loadingFeed ? (
          <div className="card">
            <Skeleton rows={6} />
          </div>
        ) : feed.length === 0 ? (
          <div className="card">
            <CaughtUp>{show === "all" ? t("You're all caught up.") : t("Nothing of this kind waits for you.")}</CaughtUp>
          </div>
        ) : (
          <div className="feed">
            {feed.slice(0, 30).map((i) => (
              <div key={i.id} className={`feed-item kind-${i.kind}`}>
                <div className="head">
                  <span className="kind-chip">{t(KIND_LABEL[i.kind])}</span>
                  {i.mine && <span className="pill status-warn">{t("asks you")}</span>}
                </div>
                {i.node}
              </div>
            ))}
            {feed.length > 30 && (
              <span className="small muted">
                {feed.length - 30} {t("more")}: <Link to="/inbox">{t("Approvals")}</Link> · <Link to="/questions">{t("Questions")}</Link> ·{" "}
                <Link to="/memory">{t("Memory")}</Link>
              </span>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

// ---- service updates in the feed ------------------------------------------------------------

function serviceUpdates(v: ServiceView | undefined): FeedItem[] {
  if (!v?.code_repo || !v.can_approve) return [];
  const out: FeedItem[] = [];
  const s = v.status;
  if (s && s.pending.length && s.upstream_sha) {
    out.push({ id: `deploy:${s.upstream_sha}`, kind: "update", at: null, node: <DeployCard view={v} /> });
  }
  for (const pr of (v.prs ?? []).filter((p) => p.self_improvement)) {
    out.push({ id: `merge:${pr.number}`, kind: "update", at: null, node: <MergeCard pr={pr} repo={v.code_repo} /> });
  }
  return out;
}

function DeployCard({ view }: { view: ServiceView }) {
  const t = useT();
  const s = view.status!;
  const deploy = useAct((sha: string) => api.post("/api/service/deploy", { sha }), [["service"], ["audit"]]);
  return (
    <div className="stack">
      <div className="row">
        <strong>
          {s.pending.length} {t("commits on")} {view.branch} {t("are not running yet")}
        </strong>
      </div>
      <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
        {s.pending.slice(0, 4).map((c) => (
          <li key={c.sha}>
            <code>{c.sha.slice(0, 8)}</code> {c.subject} <span className="muted">({c.author})</span>
          </li>
        ))}
        {s.pending.length > 4 && <li className="muted">…</li>}
      </ul>
      <div className="row">
        <button className="primary" disabled={deploy.isPending || s.dirty || deploy.isSuccess} onClick={() => deploy.mutate(s.upstream_sha!)}>
          <Icon name="upload" size={14} />
          {deploy.isSuccess ? t("Deploying…") : `${t("Deploy")} ${s.upstream_sha!.slice(0, 8)}`}
        </button>
        <Link to="/service" className="small">
          {t("Details")}
        </Link>
        {s.dirty && <span className="small muted">{t("The checkout has local changes; deploy is disabled.")}</span>}
      </div>
      <ErrorBox error={deploy.error} />
    </div>
  );
}

function MergeCard({ pr, repo }: { pr: NonNullable<ServiceView["prs"]>[number]; repo: string }) {
  const t = useT();
  const merge = useAct((n: number) => api.post(`/api/service/prs/${n}/merge`), [["service"], ["audit"]]);
  return (
    <div className="stack">
      <div className="row">
        <strong>{pr.title}</strong>
        <a href={pr.url} target="_blank" rel="noreferrer" className="small">
          {repo}#{pr.number} <Icon name="external" size={12} />
        </a>
      </div>
      <span className="small muted">
        {t("A self-improvement PR on")} <code>{pr.branch}</code>. {t("Review it on GitHub; merging needs your approval here.")}
      </span>
      <div className="row">
        <button className="primary" disabled={merge.isPending || merge.isSuccess} onClick={() => merge.mutate(pr.number)}>
          <Icon name="merge" size={14} />
          {merge.isSuccess ? t("Merged") : t("Approve and merge")}
        </button>
      </div>
      <ErrorBox error={merge.error} />
    </div>
  );
}
