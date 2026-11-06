// Home: the control center. What waits for the maintainer (agent questions, proposals,
// memory changes, service updates) in one feed they can act on in place, the state of
// the queue and the service at a glance, and the PRs worth reviewing next.

import { useMemo, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Me, type PrRow, type ReviewStart, type ServiceView } from "../api";
import { AskButton, useAgent } from "../agent";
import { CaughtUp, ErrorBox, prPath, Section, Tier, timeOf, useAct, When } from "../components";
import { prKey, useMemoryChanges, useProposals, usePrs, useQuestions, useRepos, useService } from "../data";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { ProposalCard } from "../proposal";
import { Skeleton, useTab, useToast } from "../ui";
import { ChangeCard } from "./Memory";
import { QuestionCard } from "./Questions";

const NEEDS_REVIEW = new Set(["NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS"]);

type Kind = "question" | "proposal" | "memory" | "update";

interface FeedItem {
  id: string;
  kind: Kind;
  at: string | null;
  node: ReactNode;
}

const KIND_LABEL: Record<Kind, string> = {
  question: "Agent question",
  proposal: "Proposal",
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

  const rows = prs.data ?? [];
  const needs = rows.filter((p) => NEEDS_REVIEW.has(p.review_state));
  const byTier = (tier: string) => rows.filter((p) => p.tier === tier).length;
  const repoRows = repos.data ?? [];
  const sv = service.data;
  const updates = serviceUpdates(sv);

  const items = useMemo<FeedItem[]>(() => {
    const out: FeedItem[] = [];
    for (const q of questions.data ?? []) out.push({ id: q.id, kind: "question", at: q.created_at, node: <QuestionCard question={q} compact /> });
    for (const p of proposals.data ?? []) out.push({ id: p.id, kind: "proposal", at: p.created_at, node: <ProposalCard proposal={p} compact /> });
    for (const c of memory.data ?? []) out.push({ id: c.id, kind: "memory", at: c.created_at, node: <ChangeCard change={c} compact /> });
    for (const u of updates) out.push(u);
    return out;
  }, [questions.data, proposals.data, memory.data, updates]);

  const counts: Record<Kind, number> = { question: 0, proposal: 0, memory: 0, update: 0 };
  for (const i of items) counts[i.kind] += 1;
  const tabs = [
    { id: "all", label: t("All"), count: items.length },
    { id: "question", label: t("Questions"), count: counts.question },
    { id: "proposal", label: t("Proposals"), count: counts.proposal },
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
      if ((a.kind === "question") !== (b.kind === "question")) return a.kind === "question" ? -1 : 1;
      const d = (timeOf(b.at) ?? 0) - (timeOf(a.at) ?? 0);
      return order === "old" ? -d : d;
    });
  const loadingFeed = questions.isLoading || proposals.isLoading || memory.isLoading;
  const first = (me.name || me.login).split(" ")[0];

  return (
    <>
      <div className="hero">
        <div className="titles">
          <h1>
            {t(greeting())}, {first}
          </h1>
          <p>
            {items.length
              ? `${items.length} ${t(items.length === 1 ? "decision waits for you" : "decisions wait for you")} · ${needs.length} ${t("PRs need review")}`
              : `${t("Nothing waits for your decision.")} ${needs.length} ${t("PRs need review")}.`}
          </p>
        </div>
        <div className="row">
          <Link to="/prs?tab=review" className="button">
            <Icon name="pr" />
            {t("Review queue")}
          </Link>
          <AskButton />
        </div>
      </div>

      <div className="tiles">
        <Tile
          icon="inbox"
          label={t("Waiting on you")}
          value={items.length}
          href="#decisions"
          foot={
            <>
              {counts.question} {t("questions")} · {counts.proposal} {t("proposals")} · {counts.memory} {t("memory")}
            </>
          }
        />
        <Tile
          icon="pr"
          label={t("Need review")}
          value={prs.isLoading ? "…" : needs.length}
          to="/prs?tab=review"
          foot={
            <>
              {needs.filter((p) => !p.claimed_by).length} {t("unclaimed")} · {rows.length} {t("open in total")}
            </>
          }
        />
        <Tile
          icon="activity"
          label={t("Top priority")}
          value={prs.isLoading ? "…" : byTier("P0") + byTier("P1")}
          to="/prs?f.tier=P0,P1"
          foot={
            <>
              <Tier tier="P0" /> {byTier("P0")} <Tier tier="P1" /> {byTier("P1")}
            </>
          }
        />
        <Tile
          icon="repo"
          label={t("Repositories")}
          value={repos.isLoading ? "…" : `${repoRows.filter((r) => r.active).length}/${repoRows.length}`}
          to="/repos"
          foot={
            <>
              {t("active")} · {repoRows.filter((r) => r.paused).length} {t("paused")} · {repoRows.filter((r) => r.candidate_pending).length} {t("profile changes")}
            </>
          }
        />
        <Tile
          icon={me.paused ? "pause" : "server"}
          label={t("Service")}
          value={
            <span className="row" style={{ fontSize: 20, gap: 8 }}>
              <Icon name={me.paused ? "pause" : "check"} size={20} />
              {me.paused ? t("Paused") : t("Running")}
            </span>
          }
          to="/service"
          foot={
            sv?.status ? (
              <>
                <code>{sv.status.running_sha.slice(0, 8)}</code>
                {sv.status.pending.length ? ` · ${sv.status.pending.length} ${t("updates to deploy")}` : ` · ${t("up to date")}`}
              </>
            ) : (
              t(me.dev_mode ? "dev mode" : "no self-update configured")
            )
          }
        />
      </div>

      <div className="split">
        <section id="decisions" className="stack" style={{ gap: 12, minWidth: 0 }}>
          <div className="row">
            <h2 style={{ fontSize: 17 }}>{t("Needs your decision")}</h2>
            <span className="spacer" />
            <div className="row" style={{ gap: 4 }}>
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
                  </div>
                  {i.node}
                </div>
              ))}
              {feed.length > 30 && (
                <span className="small muted">
                  {feed.length - 30} {t("more")}: <Link to="/inbox">{t("Proposals")}</Link> · <Link to="/questions">{t("Questions")}</Link> ·{" "}
                  <Link to="/memory">{t("Memory")}</Link>
                </span>
              )}
            </div>
          )}
        </section>

        <aside className="side">
          <NextUp rows={needs} loading={prs.isLoading} me={me} />
          <Section title={t("Repositories")} aside={<Link to="/repos" className="small">{t("Manage")}</Link>}>
            {repos.isLoading ? (
              <Skeleton rows={3} />
            ) : !repoRows.length ? (
              <span className="muted small">{t("No repositories yet.")} <Link to="/repos">{t("Add one")}</Link></span>
            ) : (
              <div className="list">
                {repoRows.map((r) => (
                  <Link key={r.repo} to={`/repos/${r.repo}`} className="list-row" style={{ textDecoration: "none" }}>
                    <Icon name="repo" />
                    <span className="main">
                      <span className="t">{r.repo}</span>
                      <span className="meta">
                        {r.paused ? t("paused") : r.active ? t("active") : t("waiting for an approved profile")} · {r.open_prs} {t("open PRs")}
                        {r.pending_proposals ? ` · ${r.pending_proposals} ${t("proposals")}` : ""}
                      </span>
                    </span>
                    {r.candidate_pending && <span className="pill status-warn">{t("profile change")}</span>}
                  </Link>
                ))}
              </div>
            )}
          </Section>
        </aside>
      </div>
    </>
  );
}

function Tile({
  icon,
  label,
  value,
  foot,
  to,
  href,
}: {
  icon: IconName;
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  to?: string;
  href?: string;
}) {
  const inner = (
    <>
      <span className="tile-label">
        <Icon name={icon} size={15} />
        {label}
      </span>
      <span className="tile-value">{value}</span>
      {foot && <span className="tile-foot">{foot}</span>}
    </>
  );
  if (to) {
    return (
      <Link to={to} className="tile">
        {inner}
      </Link>
    );
  }
  return (
    <a href={href} className="tile">
      {inner}
    </a>
  );
}

function NextUp({ rows, loading, me }: { rows: PrRow[]; loading: boolean; me: Me }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const navigate = useNavigate();
  const top = [...rows].sort((a, b) => (b.total ?? -1) - (a.total ?? -1)).slice(0, 6);
  const review = useAct((p: PrRow) => api.post<ReviewStart[]>("/api/reviews", { prs: [{ repo: p.repo, number: p.number }] }), [["prs"]]);
  return (
    <Section title={t("Review next")} aside={<Link to="/prs?tab=review" className="small">{t("All")} ({rows.length})</Link>}>
      {loading ? (
        <Skeleton rows={4} />
      ) : !top.length ? (
        <span className="muted small">{t("No PR needs a review.")}</span>
      ) : (
        <div className="list">
          {top.map((p) => {
            const busy = p.review_activity.job && ["queued", "running"].includes(p.review_activity.job.status);
            return (
              <div key={prKey(p)} className="list-row">
                <Tier tier={p.tier} />
                <Link to={prPath(p.repo, p.number)} className="main" style={{ textDecoration: "none" }}>
                  <span className="t wrap">{p.title}</span>
                  <span className="meta">
                    {p.repo}#{p.number} · {p.author} · <When at={p.updated_at} />
                    {p.claimed_by ? ` · ${p.claimed_by === me.login ? t("claimed by you") : `${t("claimed by")} ${p.claimed_by}`}` : ""}
                  </span>
                </Link>
                <span className="row-actions">
                  <button
                    className="sm"
                    disabled={!!busy || review.isPending}
                    title={t("Draft a review into your proposals")}
                    onClick={() =>
                      review.mutate(p, {
                        onSuccess: (out) =>
                          toast({
                            title: out[0]?.run === "skip" ? t("Nothing to review") : t("Review started"),
                            body: out[0]?.run === "skip" ? out[0].reason : `${p.repo}#${p.number}: ${t("the draft will appear in your proposals")}`,
                            icon: "pr",
                            action: { label: t("Open"), onClick: () => navigate(prPath(p.repo, p.number)) },
                          }),
                      })
                    }
                  >
                    {busy ? t("Reviewing…") : t("Review")}
                  </button>
                  <button
                    className="ghost icon sm"
                    aria-label={t("Ask agent")}
                    title={t("Ask agent")}
                    onClick={() => agent.open({ context: { page: "pr", repo: p.repo, number: p.number, label: `${p.repo}#${p.number}` } })}
                  >
                    <Icon name="sparkles" size={14} />
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      )}
      <ErrorBox error={review.error} />
    </Section>
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
