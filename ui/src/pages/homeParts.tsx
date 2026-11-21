// Home's working parts: what a first-time user does first, the PRs to review with one
// click (and which agent reviews them), and when reviews start on their own.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type AgentJob, type ChatThread, type Me, type PrRow, type RepoRow, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { agentName, AgentPicker, reviewToast, useAgentChoice } from "../agentPicker";
import { ErrorBox, prPath, Quality, Tier, timeOf, useAct, When } from "../components";
import { priorityRank, prKey, REVIEW_STATES, useSettings } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { ACTIVE, needsMe, ScoringRow, useWork, WorkRow } from "../jobs";
import { openJob } from "../openJob";
import { Skeleton, useToast } from "../ui";
import { holds, Person, PrPeopleLine } from "../avatar";
import { useAliasOf } from "../aliases";
import { ProgressInline } from "../progress";
import { isHomeOrder, resolveHomeOrder, writeHomeOrder, type HomeOrder } from "../viewState";

// ---- getting started ---------------------------------------------------------------------------

function readHidden(): boolean {
  try {
    return localStorage.getItem("am.start.hidden") === "1";
  } catch {
    return false;
  }
}

export function GettingStarted({ repos, prs }: { repos: RepoRow[]; prs: PrRow[] }) {
  const t = useT();
  const agent = useAgent();
  const navigate = useNavigate();
  const [hidden, setHidden] = useState(readHidden);
  const settings = useSettings(!hidden);
  const threads = useQuery({ queryKey: ["chat", "threads"], queryFn: () => api.get<ChatThread[]>("/api/chat/threads"), enabled: !hidden });
  const reviewed = prs.some((p) => p.review_activity.posted || p.review_activity.draft || p.review_activity.job);
  const pending = repos.find((r) => !r.active);
  const steps: { done: boolean; title: string; body: string; action: string; run: () => void }[] = [
    {
      done: repos.length > 0,
      title: t("Add a repository"),
      body: t("An agent reads it and drafts its profile: labels, checks, review style."),
      action: t("Add"),
      run: () => navigate("/repos?add=1"),
    },
    {
      done: repos.length > 0 && !pending,
      title: t("Approve its profile"),
      body: t("Nothing is posted until a maintainer approves how the service works there."),
      action: t("Review it"),
      run: () => navigate(pending ? `/repos/${pending.repo}?tab=candidate` : "/repos"),
    },
    {
      done: reviewed,
      title: t("Review your first pull requests"),
      body: t("Select PRs below and press Review; the drafts come back to you."),
      action: t("Go"),
      run: () => document.getElementById("review")?.scrollIntoView({ behavior: "smooth" }),
    },
    {
      done: prs.some((p) => p.review_activity.posted),
      title: t("Discuss a draft with the agent"),
      body: t("Open a review's live view: ask why it found something, push back, or have it revise the draft, then post it."),
      action: t("Open"),
      run: () => document.getElementById("work")?.scrollIntoView({ behavior: "smooth" }),
    },
    {
      // Someone who does not run the service has nothing to pick here.
      done: settings.data ? settings.data.document === null || !!settings.data.document.trim().replace(/^schema: 1\s*$/, "") : false,
      title: t("Pick agents and models"),
      body: t("Which agents run and on which model providers. Ask the agent to change it."),
      action: t("Open"),
      run: () => navigate("/settings"),
    },
    {
      done: (threads.data ?? []).length > 0,
      title: t("Ask the agent something"),
      body: t("It is on every page and knows what the page shows."),
      action: t("Ask"),
      run: () => agent.open(),
    },
  ];
  const left = steps.filter((s) => !s.done).length;
  if (hidden || left === 0) return null;
  return (
    <section className="card start">
      <div className="row" style={{ marginBottom: 10 }}>
        <Icon name="sparkles" />
        <strong style={{ fontSize: 15 }}>{t("Get started")}</strong>
        <span className="small muted">
          {steps.length - left}/{steps.length} {t("done")}
        </span>
        <button
          className="ghost sm right"
          onClick={() => {
            setHidden(true);
            try {
              localStorage.setItem("am.start.hidden", "1");
            } catch {
              // the checklist then hides for this page only
            }
          }}
        >
          {t("Hide")}
        </button>
      </div>
      <div className="steps">
        {steps.map((s, i) => (
          <div key={s.title} className={`start-step ${s.done ? "done" : ""}`}>
            <span className={`step-dot ${s.done ? "done" : ""}`}>{s.done ? <Icon name="check" size={13} /> : i + 1}</span>
            <span className="stack" style={{ gap: 2, flex: 1, minWidth: 0 }}>
              <strong>{s.title}</strong>
              <span className="small muted">{s.body}</span>
            </span>
            {!s.done && (
              <button className="sm" onClick={s.run}>
                {s.action}
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ---- review with one click ----------------------------------------------------------------------

function activity(p: PrRow, me: Me, t: (s: string) => string): ReactNode {
  const a = p.review_activity;
  const job = a.job;
  if (job && ["queued", "running", "waiting"].includes(job.status)) {
    const who = job.by ? (job.by === me.login ? t("you") : job.by) : null;
    return (
      <button
        className="link-button"
        style={{ color: "var(--info)" }}
        title={t("Watch the agent work")}
        onClick={(e) => {
          e.preventDefault();
          openJob(job.id);
        }}
      >
        <span className="pulse" /> {job.playbook === "followup" ? t("following up…") : t("reviewing…")}
        {job.agent ? ` ${agentName(job.agent)}` : ""}
        {who ? ` · ${who}` : ""}
      </button>
    );
  }
  if (a.draft) {
    return (
      <Link to="/inbox">
        {!a.draft.for ? t("draft") : a.draft.for === me.login ? t("draft for you") : `${t("draft for")} ${a.draft.for}`} →
      </Link>
    );
  }
  if (job?.status === "failed") return <span style={{ color: "var(--bad)" }}>{t("review failed")}</span>;
  return null;
}

/** Whether a PR matches every word of a search: its title, repository and number,
 *  author, labels, summary, and the reviewers on it. */
function matches(p: PrRow, words: string[], aliasOf: (login: string) => string | undefined): boolean {
  const names = [p.author, ...p.assignees.map((a) => a.login)].map((l) => `${l} ${aliasOf(l) ?? ""}`).join(" ");
  const hay = `${p.title} ${p.repo}#${p.number} #${p.number} ${p.labels.join(" ")} ${p.summary} ${names}`.toLowerCase();
  return words.every((w) => hay.includes(w));
}

type Staffed = "" | "owner" | "no-owner" | "reviewers" | "no-reviewers";

/** Home's choice of PRs by whether someone owns or reviews them here. */
const STAFFED: Record<Staffed, { label: string; keep: (p: PrRow) => boolean }> = {
  "": { label: "With or without people", keep: () => true },
  owner: { label: "Has an owner", keep: (p) => p.assignees.some((a) => a.role === "owner") },
  "no-owner": { label: "No owner yet", keep: (p) => !p.assignees.some((a) => a.role === "owner") },
  reviewers: { label: "Has reviewers", keep: (p) => p.assignees.some((a) => a.role === "reviewer") },
  "no-reviewers": { label: "No reviewers yet", keep: (p) => !p.assignees.some((a) => a.role === "reviewer") },
};

type Needed = "" | "first" | "followup";

/** Home's choice of PRs by the review they wait for: a first one, or a follow-up of
 *  the service's last review (new commits since; after an approval too). */
const NEEDED: Record<Needed, { label: string; keep: (p: PrRow) => boolean }> = {
  "": { label: "Any review", keep: () => true },
  first: { label: "First review", keep: (p) => p.review_state === "NEW_REVIEW" },
  followup: { label: "Follow-up review", keep: (p) => p.review_state === "FOLLOWUP_REVIEW" || p.review_state === "APPROVED_NEW_COMMITS" },
};

type ReviewOrder = HomeOrder;

const readiness = (p: PrRow) => p.progress?.completeness?.value ?? 0;

/** The orders Home's PRs to review offers: the repository's priorities (the default),
 *  how close each is to ready, or time, by when a PR was opened or last updated; PRs
 *  without the time go last. */
const REVIEW_ORDERS: Record<ReviewOrder, { label: string; compare: (a: PrRow, b: PrRow) => number }> = {
  priority: { label: "Priority", compare: (a, b) => priorityRank(b) - priorityRank(a) },
  // The PRs closest to ready first (reviewed, approved, resolved), then by priority.
  ready: { label: "Closest to ready", compare: (a, b) => readiness(b) - readiness(a) || priorityRank(b) - priorityRank(a) },
  newest: { label: "Newest first", compare: (a, b) => (timeOf(b.created_at) ?? -Infinity) - (timeOf(a.created_at) ?? -Infinity) },
  oldest: { label: "Oldest first", compare: (a, b) => (timeOf(a.created_at) ?? Infinity) - (timeOf(b.created_at) ?? Infinity) },
  updated: { label: "Recently updated", compare: (a, b) => (timeOf(b.updated_at) ?? -Infinity) - (timeOf(a.updated_at) ?? -Infinity) },
};

/** PRs that need a review in one repository, ranked by its priorities. A search looks
 *  through every open PR (``all``), not only those waiting for a review. */
export function ReviewCard({
  rows,
  all,
  loading,
  me,
  searchParams,
}: {
  rows: PrRow[];
  all: PrRow[];
  loading: boolean;
  me: Me;
  searchParams?: URLSearchParams;
}) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const navigate = useNavigate();
  const [urlParams, setParams] = useSearchParams();
  const params = searchParams ?? urlParams;
  const updateParams = (changes: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(changes)) {
          if (v === null || v === "") next.delete(k);
          else next.set(k, v);
        }
        return next;
      },
      { replace: true },
    );
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [who, setWho] = useAgentChoice("review");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const query = params.get("rq") ?? "";
  const rawOnly = params.get("only");
  const only: "owner" | "reviewer" | null = rawOnly === "owner" || rawOnly === "reviewer" ? rawOnly : null;
  const rawStaffed = params.get("staffed") ?? "";
  const staffed: Staffed = rawStaffed in STAFFED ? (rawStaffed as Staffed) : "";
  const rawNeed = params.get("need") ?? "";
  const need: Needed = rawNeed in NEEDED ? (rawNeed as Needed) : "";
  const order = resolveHomeOrder(params);
  const urlOrder = params.get("rorder");
  useEffect(() => {
    if (isHomeOrder(urlOrder)) writeHomeOrder(urlOrder);
  }, [urlOrder]);
  const showAll = params.get("rall") === "1";
  const setQuery = (v: string) => updateParams({ rq: v || null });
  const setOnly = (v: "owner" | "reviewer" | null) => updateParams({ only: v });
  const setStaffed = (v: Staffed) => updateParams({ staffed: v || null });
  const setNeed = (v: Needed) => updateParams({ need: v || null });
  const setOrder = (o: ReviewOrder) => {
    writeHomeOrder(o);
    updateParams({ rorder: o });
  };
  const setShowAll = (v: boolean) => updateParams({ rall: v ? "1" : null });
  const filtering = Boolean(query || only || staffed || need);
  const extraFilterCount = (staffed ? 1 : 0) + (need ? 1 : 0);
  const aliasOf = useAliasOf();
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const searching = words.length > 0;
  // The PRs one owns or reviews matter whatever their review state, so these two
  // filters look through every open PR, not only those waiting for a review.
  const ownedCount = all.filter((p) => holds(p.assignees, me.login, "owner")).length;
  const reviewingCount = all.filter((p) => holds(p.assignees, me.login, "reviewer")).length;
  const pool = (searching ? all.filter((p) => matches(p, words, aliasOf)) : only ? all : rows).filter(
    (p) => (!only || holds(p.assignees, me.login, only)) && STAFFED[staffed].keep(p) && NEEDED[need].keep(p),
  );
  const sorted = [...pool].sort(REVIEW_ORDERS[order].compare);
  const shown = showAll || searching ? sorted : sorted.slice(0, 8);
  const waiting = new Set(rows.map(prKey));
  const review = useAct(
    (prs: PrRow[]) => api.post<ReviewStart[]>("/api/reviews", { prs: prs.map((p) => ({ repo: p.repo, number: p.number })), agent: who || null }),
    [["prs"], ["proposals"]],
  );
  const chosen = sorted.filter((p) => selected.has(prKey(p)));
  const start = (prs: PrRow[]) =>
    review.mutate(prs, {
      onSuccess: (out) => {
        setSelected(new Set());
        toast(reviewToast(out, t, () => navigate("/inbox")));
      },
    });
  const allChecked = shown.length > 0 && shown.every((p) => selected.has(prKey(p)));
  return (
    <section id="review" className="card" data-tour="home-review">
      <div className="review-card-head">
        <div className="review-card-title">
          <input
            type="checkbox"
            aria-label={t("Select all")}
            checked={allChecked}
            onChange={() => setSelected(allChecked ? new Set() : new Set(shown.map(prKey)))}
          />
          <h2>{t("Pull requests to review")}</h2>
          <span className="small muted" title={searching ? t("PRs that match, of the open PRs") : undefined}>
            {searching || only ? `${sorted.length} / ${all.length}` : need ? `${sorted.length} / ${rows.length}` : rows.length}
          </span>
        </div>
        <div className="review-card-actions">
          <AgentPicker value={who} onChange={setWho} />
          <button className="primary" disabled={!chosen.length || review.isPending} onClick={() => start(chosen)}>
            <Icon name="pr" size={15} />
            {review.isPending ? t("Starting…") : chosen.length ? `${t("Review")} ${chosen.length}` : t("Review selected")}
          </button>
        </div>
      </div>
      <div className={`review-filters${filtersOpen || extraFilterCount > 0 ? " expanded" : ""}`}>
        <label className="search review-search">
          <Icon name="search" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQuery("")}
            placeholder={t("Search open PRs: title, #number, author, label, reviewer")}
            aria-label={t("Search open PRs")}
          />
          {query && (
            <button type="button" className="ghost icon sm" aria-label={t("Clear the search")} onClick={() => setQuery("")}>
              <Icon name="x" size={13} />
            </button>
          )}
        </label>
        <div className="review-filter-bar">
          <button
            type="button"
            className={`chip-btn ${only === "owner" ? "on" : ""}`}
            aria-pressed={only === "owner"}
            title={t("PRs you own here: you answer for their progress")}
            onClick={() => setOnly(only === "owner" ? null : "owner")}
            style={{ borderStyle: "solid", height: 34 }}
          >
            {t("Owned by me")} {ownedCount > 0 && <span className="n">{ownedCount}</span>}
          </button>
          <button
            type="button"
            className={`chip-btn ${only === "reviewer" ? "on" : ""}`}
            aria-pressed={only === "reviewer"}
            title={t("PRs you review here; a review GitHub requested of you does not count")}
            onClick={() => setOnly(only === "reviewer" ? null : "reviewer")}
            style={{ borderStyle: "solid", height: 34 }}
          >
            {t("I review")} {reviewingCount > 0 && <span className="n">{reviewingCount}</span>}
          </button>
          <button
            type="button"
            className={`chip-btn review-filter-toggle ${filtersOpen || extraFilterCount > 0 ? "on" : ""}`}
            aria-expanded={filtersOpen || extraFilterCount > 0}
            onClick={() => setFiltersOpen((o) => !o)}
            style={{ borderStyle: "solid", height: 34 }}
          >
            <Icon name="filter" size={14} />
            {t("Filter")}
            {extraFilterCount > 0 && <span className="n">{extraFilterCount}</span>}
          </button>
          <select value={staffed} onChange={(e) => setStaffed(e.target.value as Staffed)} aria-label={t("Owners and reviewers")} title={t("Owners and reviewers")} className="review-extra-filter">
            {(Object.keys(STAFFED) as Staffed[]).map((k) => (
              <option key={k} value={k}>
                {t(STAFFED[k].label)}
              </option>
            ))}
          </select>
          <select value={need} onChange={(e) => setNeed(e.target.value as Needed)} aria-label={t("Review needed")} title={t("Review needed")} className="review-extra-filter">
            {(Object.keys(NEEDED) as Needed[]).map((k) => {
              const n = rows.filter(NEEDED[k].keep).length;
              return (
                <option key={k} value={k}>
                  {t(NEEDED[k].label)}
                  {n ? ` (${n})` : ""}
                </option>
              );
            })}
          </select>
          {filtering && (
            <button type="button" className="ghost sm" onClick={() => updateParams({ rq: null, only: null, staffed: null, need: null })}>
              {t("Reset")}
            </button>
          )}
          <select
            value={order}
            onChange={(e) => setOrder(e.target.value as ReviewOrder)}
            aria-label={t("Sort by")}
            title={t("Sort by")}
            className="review-sort"
          >
            {(Object.keys(REVIEW_ORDERS) as ReviewOrder[]).map((k) => (
              <option key={k} value={k}>
                {t(REVIEW_ORDERS[k].label)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {loading ? (
        <Skeleton rows={4} />
      ) : searching && !sorted.length ? (
        <span className="small muted">{t("No open PR matches the search.")}</span>
      ) : !pool.length ? (
        <span className="small muted">
          {only === "owner" ? t("You own no open PR here.") : only === "reviewer" ? t("You review no open PR here.") : t("No PR needs a review.")}
        </span>
      ) : (
        <div className="list">
          {shown.map((p) => (
            <label key={prKey(p)} className="list-row pr-row" style={{ cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={selected.has(prKey(p))}
                onChange={() =>
                  setSelected((prev) => {
                    const next = new Set(prev);
                    if (next.has(prKey(p))) next.delete(prKey(p));
                    else next.add(prKey(p));
                    return next;
                  })
                }
              />
              <Tier tier={p.tier} />
              <span className="main">
                <Link to={prPath(p.repo, p.number)} state={{ from: "home" }} className="t" style={{ color: "var(--text)" }} onClick={(e) => e.stopPropagation()}>
                  {p.title}
                </Link>
                <span className="meta">
                  #{p.number} · <Person login={p.author} /> · <When at={p.updated_at} />
                </span>
                {p.assignees.length > 0 && (
                  <span className="meta">
                    <PrPeopleLine people={p.assignees} me={me.login} t={t} />
                  </span>
                )}
              </span>
              <ProgressInline p={p.progress} />
              {(p.quality || p.quick_quality) && <Quality quality={p.quality} quick={p.quick_quality} head={p.head} compact />}
              {(p.review_state === "FOLLOWUP_REVIEW" || p.review_state === "APPROVED_NEW_COMMITS") && (
                <span className="pill status-warn" title={t("New commits since the last review")}>
                  {t("Needs follow-up")}
                </span>
              )}
              {searching && !waiting.has(prKey(p)) && <span className="pill tag">{t(REVIEW_STATES[p.review_state] ?? p.review_state)}</span>}
              <span className="small">{activity(p, me, t)}</span>
              <span className="row-actions">
                <button
                  className="ghost icon sm"
                  title={t("Ask agent")}
                  aria-label={t("Ask agent")}
                  onClick={(e) => {
                    e.preventDefault();
                    agent.open({ context: { page: "pr", repo: p.repo, number: p.number, label: `${p.repo}#${p.number}` } });
                  }}
                >
                  <Icon name="sparkles" size={14} />
                </button>
              </span>
            </label>
          ))}
        </div>
      )}
      {!searching && rows.length > 8 && (
        <button className="ghost sm" style={{ marginTop: 8 }} onClick={() => setShowAll(!showAll)}>
          {showAll ? t("Show fewer") : `${t("Show all")} (${rows.length})`}
        </button>
      )}
      <ErrorBox error={review.error} />
    </section>
  );
}

// ---- what agents are doing ------------------------------------------------------------------

export function AgentWork({ me }: { me: Me }) {
  const t = useT();
  const [everyone, setEveryone] = useState(false);
  const work = useWork(everyone);
  const jobs = work.data ?? [];
  // Scoring jobs come in batches (every PR of a repository scored again): one line per
  // repository, unless one waits for the person.
  const scoring = jobs.filter((j) => j.playbook === "evaluate" && !needsMe(j, me.login));
  const others = jobs.filter((j) => !scoring.includes(j));
  const byRepo = new Map<string, AgentJob[]>();
  for (const j of scoring) byRepo.set(j.repo ?? "", [...(byRepo.get(j.repo ?? "") ?? []), j]);
  const batches = [...byRepo.entries()].map(([repo, list]) => ({ repo, jobs: list, active: list.some((j) => ACTIVE.has(j.status)) }));
  const groups = {
    needs: others.filter((j) => needsMe(j, me.login)),
    running: others.filter((j) => ACTIVE.has(j.status) && !needsMe(j, me.login)),
    done: others.filter((j) => !ACTIVE.has(j.status) && !needsMe(j, me.login)),
  };
  const scoringIn = {
    needs: [] as typeof batches,
    running: batches.filter((b) => b.active),
    done: batches.filter((b) => !b.active),
  };
  const size = (id: keyof typeof groups) => groups[id].length + scoringIn[id].length;
  // The tab is chosen once, when the list first loads (what waits for the person,
  // else what is at work), and stays until they pick another: a job ending must not
  // move the section to Finished under them.
  const [tab, setTab] = useState<keyof typeof groups | null>(null);
  const first = work.data ? (size("needs") ? "needs" : "running") : null;
  useEffect(() => {
    if (tab === null && first !== null) setTab(first);
  }, [tab, first]);
  const shown = tab ?? first ?? "running";
  const [all, setAll] = useState(false);
  const rows = groups[shown];
  const tabs: { id: keyof typeof groups; label: string }[] = [
    { id: "needs", label: t("Needs you") },
    { id: "running", label: t("Working") },
    { id: "done", label: t("Finished") },
  ];
  return (
    <section id="work" className="card" data-tour="home-work">
      <div className="work-head">
        <h2>{t("Agents' work")}</h2>
        <div className="segmented work-tabs" role="tablist">
          {tabs.map((x) => (
            <button key={x.id} role="tab" aria-selected={shown === x.id} className={shown === x.id ? "on" : ""} onClick={() => setTab(x.id)}>
              {x.label}
              {size(x.id) ? <span className="muted"> {size(x.id)}</span> : null}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <button className={`chip-btn work-scope ${everyone ? "on" : ""}`} style={{ borderStyle: "solid" }} onClick={() => setEveryone(!everyone)}>
          {everyone ? t("Everyone's") : t("Mine")}
        </button>
      </div>
      {work.isLoading ? (
        <Skeleton rows={2} />
      ) : !size(shown) ? (
        <span className="small muted">
          {shown === "needs"
            ? t("No agent is waiting for you.")
            : shown === "running"
              ? t("No agent is working for you right now. Start a review below.")
              : t("Nothing finished in the last week.")}
        </span>
      ) : (
        <div className="list">
          {scoringIn[shown].map((b) => (
            <ScoringRow key={b.repo} repo={b.repo} jobs={b.jobs} />
          ))}
          {(all ? rows : rows.slice(0, 6)).map((j) => (
            <WorkRow key={j.id} job={j} me={me.login} />
          ))}
        </div>
      )}
      {rows.length > 6 && (
        <button className="ghost sm" style={{ marginTop: 6 }} onClick={() => setAll(!all)}>
          {all ? t("Show fewer") : `${t("Show all")} (${rows.length})`}
        </button>
      )}
      <ErrorBox error={work.error} />
    </section>
  );
}

