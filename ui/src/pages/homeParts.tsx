// Home's working parts: what a first-time user does first, the PRs to review with one
// click (and which agent reviews them), and when reviews start on their own.

import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type AgentJob, type Automation, type ChatThread, type Me, type PrRow, type RepoRow, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { agentName, AgentPicker, reviewToast, useAgentChoice } from "../agentPicker";
import { ErrorBox, prPath, Quality, Tier, timeOf, useAct, When } from "../components";
import { priorityRank, prKey, REVIEW_STATES, useSettings } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { ACTIVE, needsMe, ScoringRow, useWork, WorkRow } from "../jobs";
import { openJob } from "../openJob";
import { Popover, Skeleton, useToast } from "../ui";
import { holds, Person, PrPeopleLine } from "../avatar";
import { useAliasOf } from "../aliases";
import { ProgressInline } from "../progress";

// ---- getting started ---------------------------------------------------------------------------

function readHidden(): boolean {
  try {
    return localStorage.getItem("am.start.hidden") === "1";
  } catch {
    return false;
  }
}

export function GettingStarted({ repos, prs, automations }: { repos: RepoRow[]; prs: PrRow[]; automations: Automation[] }) {
  const t = useT();
  const agent = useAgent();
  const navigate = useNavigate();
  const settings = useSettings();
  const threads = useQuery({ queryKey: ["chat", "threads"], queryFn: () => api.get<ChatThread[]>("/api/chat/threads") });
  const [hidden, setHidden] = useState(readHidden);
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
      done: automations.some((a) => a.trigger !== "manual"),
      title: t("Choose when reviews run"),
      body: t("Only when asked, every few hours, or on every update."),
      action: t("Set"),
      run: () => document.getElementById("automation")?.scrollIntoView({ behavior: "smooth" }),
    },
    {
      done: !!settings.data?.document.trim().replace(/^schema: 1\s*$/, ""),
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

type ReviewOrder = "priority" | "newest" | "oldest" | "updated";

/** The orders Home's PRs to review offers: the repository's priorities (the default),
 *  or time, by when a PR was opened or last updated; PRs without the time go last. */
const REVIEW_ORDERS: Record<ReviewOrder, { label: string; compare: (a: PrRow, b: PrRow) => number }> = {
  priority: { label: "Priority", compare: (a, b) => priorityRank(b) - priorityRank(a) },
  newest: { label: "Newest first", compare: (a, b) => (timeOf(b.created_at) ?? -Infinity) - (timeOf(a.created_at) ?? -Infinity) },
  oldest: { label: "Oldest first", compare: (a, b) => (timeOf(a.created_at) ?? Infinity) - (timeOf(b.created_at) ?? Infinity) },
  updated: { label: "Recently updated", compare: (a, b) => (timeOf(b.updated_at) ?? -Infinity) - (timeOf(a.updated_at) ?? -Infinity) },
};

/** The order chosen for Home's PRs to review, remembered in this browser. */
function useReviewOrder(): [ReviewOrder, (o: ReviewOrder) => void] {
  const [order, setOrder] = useState<ReviewOrder>(() => {
    try {
      const saved = localStorage.getItem("am.home.order") as ReviewOrder | null;
      return saved && saved in REVIEW_ORDERS ? saved : "priority";
    } catch {
      return "priority";
    }
  });
  const choose = (o: ReviewOrder) => {
    setOrder(o);
    try {
      localStorage.setItem("am.home.order", o);
    } catch {
      /* the choice lasts for this page only */
    }
  };
  return [order, choose];
}

/** PRs that need a review in one repository, ranked by its priorities. A search looks
 *  through every open PR (``all``), not only those waiting for a review. */
export function ReviewCard({ rows, all, loading, me, automations }: { rows: PrRow[]; all: PrRow[]; loading: boolean; me: Me; automations: Automation[] }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [who, setWho] = useAgentChoice("review");
  const [showAll, setShowAll] = useState(false);
  const [query, setQuery] = useState("");
  const [only, setOnly] = useState<"owner" | "reviewer" | null>(null);
  const [staffed, setStaffed] = useState<Staffed>("");
  const aliasOf = useAliasOf();
  const [order, setOrder] = useReviewOrder();
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const searching = words.length > 0;
  // The PRs one owns or reviews matter whatever their review state, so these two
  // filters look through every open PR, not only those waiting for a review.
  const ownedCount = all.filter((p) => holds(p.assignees, me.login, "owner")).length;
  const reviewingCount = all.filter((p) => holds(p.assignees, me.login, "reviewer")).length;
  const pool = (searching ? all.filter((p) => matches(p, words, aliasOf)) : only ? all : rows).filter((p) => (!only || holds(p.assignees, me.login, only)) && STAFFED[staffed].keep(p));
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
    <section id="review" className="card">
      <div className="row" style={{ marginBottom: 10 }}>
        <input
          type="checkbox"
          aria-label={t("Select all")}
          checked={allChecked}
          onChange={() => setSelected(allChecked ? new Set() : new Set(shown.map(prKey)))}
        />
        <h2>{t("Pull requests to review")}</h2>
        <span className="small muted" title={searching ? t("PRs that match, of the open PRs") : undefined}>
          {searching || only ? `${sorted.length} / ${all.length}` : rows.length}
        </span>
        <span className="spacer" />
        <AgentPicker value={who} onChange={setWho} />
        <button className="primary" disabled={!chosen.length || review.isPending} onClick={() => start(chosen)}>
          <Icon name="pr" size={15} />
          {review.isPending ? t("Starting…") : chosen.length ? `${t("Review")} ${chosen.length}` : t("Review selected")}
        </button>
      </div>
      <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
      <label className="search" style={{ flex: 1 }}>
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
        <button
          type="button"
          className={`chip-btn ${only === "owner" ? "on" : ""}`}
          aria-pressed={only === "owner"}
          title={t("PRs you own here: you answer for their progress")}
          onClick={() => setOnly(only === "owner" ? null : "owner")}
          style={{ flex: "none", borderStyle: "solid", height: 34 }}
        >
          {t("Owned by me")} {ownedCount > 0 && <span className="n">{ownedCount}</span>}
        </button>
        <button
          type="button"
          className={`chip-btn ${only === "reviewer" ? "on" : ""}`}
          aria-pressed={only === "reviewer"}
          title={t("PRs you review here; a review GitHub requested of you does not count")}
          onClick={() => setOnly(only === "reviewer" ? null : "reviewer")}
          style={{ flex: "none", borderStyle: "solid", height: 34 }}
        >
          {t("I review")} {reviewingCount > 0 && <span className="n">{reviewingCount}</span>}
        </button>
        <select value={staffed} onChange={(e) => setStaffed(e.target.value as Staffed)} aria-label={t("Owners and reviewers")} title={t("Owners and reviewers")} style={{ flex: "none", height: 34, width: "auto" }}>
          {(Object.keys(STAFFED) as Staffed[]).map((k) => (
            <option key={k} value={k}>
              {t(STAFFED[k].label)}
            </option>
          ))}
        </select>
        <select
          value={order}
          onChange={(e) => setOrder(e.target.value as ReviewOrder)}
          aria-label={t("Sort by")}
          title={t("Sort by")}
          style={{ flex: "none", height: 34, width: "auto" }}
        >
          {(Object.keys(REVIEW_ORDERS) as ReviewOrder[]).map((k) => (
            <option key={k} value={k}>
              {t(REVIEW_ORDERS[k].label)}
            </option>
          ))}
        </select>
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
                <Link to={prPath(p.repo, p.number)} className="t" style={{ color: "var(--text)" }} onClick={(e) => e.stopPropagation()}>
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
      <ScheduleLine automations={automations} />
      <ErrorBox error={review.error} />
    </section>
  );
}

// ---- when reviews start on their own ---------------------------------------------------------------

const EVERY = [1, 2, 3, 4, 6, 8, 12, 24, 168];

export function useAutomations(repos: RepoRow[]) {
  const results = useQueries({
    queries: repos
      .filter((r) => r.active)
      .map((r) => ({
        queryKey: ["automation", r.repo],
        queryFn: () => api.get<Automation>(`/api/repos/${r.repo}/review-automation`),
      })),
  });
  return results.map((q) => q.data).filter((a): a is Automation => !!a);
}

/** When reviews run in each repository, in one line, each changeable in place. */
function ScheduleLine({ automations }: { automations: Automation[] }) {
  const t = useT();
  const say = (a: Automation) =>
    a.trigger === "scan"
      ? `${t("every")} ${a.every_hours === 24 ? t("day") : a.every_hours === 168 ? t("week") : `${a.every_hours ?? 6} ${t("hours")}`}`
      : a.trigger === "events"
        ? t("on every update")
        : t("only when asked");
  return (
    <div id="automation" className="schedule-line small">
      <Icon name="clock" size={13} />
      <span className="muted">{t("Reviews run")}:</span>
      {automations.length === 0 && <span className="muted">{t("Add a repository and approve its profile first.")}</span>}
      {automations.map((a) => (
        <Popover
          key={a.repo}
          label={t("When reviews run")}
          trigger={(open, toggle) => (
            <button className={`chip-btn ${open ? "on" : ""}`} onClick={toggle} aria-expanded={open}>
              {automations.length > 1 ? `${a.repo}: ` : ""}
              {say(a)}
              {a.trigger !== "manual" && a.followups ? ` · ${t("re-review on new commits")}` : ""}
              <Icon name="chevronDown" size={12} />
            </button>
          )}
        >
          {() => (
            <div style={{ padding: 8, width: "min(360px, calc(100vw - 24px))", maxWidth: "100%" }}>
              <AutomationRow a={a} />
            </div>
          )}
        </Popover>
      ))}
    </div>
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
  const [tab, setTab] = useState<keyof typeof groups | null>(null);
  const shown = tab ?? (size("needs") ? "needs" : size("running") || !size("done") ? "running" : "done");
  const [all, setAll] = useState(false);
  const rows = groups[shown];
  const tabs: { id: keyof typeof groups; label: string }[] = [
    { id: "needs", label: t("Needs you") },
    { id: "running", label: t("Working") },
    { id: "done", label: t("Finished") },
  ];
  return (
    <section id="work" className="card">
      <div className="row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
        <h2>{t("Agents' work")}</h2>
        <div className="segmented" role="tablist">
          {tabs.map((x) => (
            <button key={x.id} role="tab" aria-selected={shown === x.id} className={shown === x.id ? "on" : ""} onClick={() => setTab(x.id)}>
              {x.label}
              {size(x.id) ? <span className="muted"> {size(x.id)}</span> : null}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <button className={`chip-btn ${everyone ? "on" : ""}`} style={{ borderStyle: "solid" }} onClick={() => setEveryone(!everyone)}>
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

function AutomationRow({ a }: { a: Automation }) {
  const t = useT();
  const toast = useToast();
  const client = useQueryClient();
  const [trigger, setTrigger] = useState(a.trigger);
  const [every, setEvery] = useState(a.every_hours ?? 6);
  const [maxPrs, setMaxPrs] = useState(a.max_prs);
  const [followups, setFollowups] = useState(a.followups);
  useEffect(() => {
    setTrigger(a.trigger);
    setEvery(a.every_hours ?? 6);
    setMaxPrs(a.max_prs);
    setFollowups(a.followups);
  }, [a]);
  const save = useAct(
    () => api.put<{ applied: boolean; reason: string }>(`/api/repos/${a.repo}/review-automation`, { trigger, every_hours: every, max_prs: maxPrs, followups }),
    [["automation"], ["memory"], ["profile"]],
  );
  const changed = trigger !== a.trigger || (trigger === "scan" && (every !== (a.every_hours ?? 6) || maxPrs !== a.max_prs)) || followups !== a.followups;
  const modes: { id: Automation["trigger"]; label: string }[] = [
    { id: "manual", label: t("When asked") },
    { id: "scan", label: t("Periodically") },
    { id: "events", label: t("On every update") },
  ];
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row">
        <Icon name="repo" size={14} />
        <strong className="small">{a.repo}</strong>
      </div>
      <div className="segmented" role="radiogroup" aria-label={t("When reviews run")}>
        {modes.map((m) => (
          <button key={m.id} role="radio" aria-checked={trigger === m.id} className={trigger === m.id ? "on" : ""} onClick={() => setTrigger(m.id)}>
            {m.label}
          </button>
        ))}
      </div>
      {trigger === "scan" && (
        <div className="row small">
          {t("Every")}
          <select value={every} onChange={(e) => setEvery(Number(e.target.value))} style={{ height: 30 }}>
            {EVERY.map((h) => (
              <option key={h} value={h}>
                {h === 168 ? t("week") : h === 24 ? t("day") : `${h} ${t("hours")}`}
              </option>
            ))}
          </select>
          {t("up to")}
          <input type="number" min={1} max={50} value={maxPrs} onChange={(e) => setMaxPrs(Number(e.target.value) || 1)} style={{ width: 64, height: 30 }} />
          {t("PRs, highest priority first")}
        </div>
      )}
      {trigger !== "manual" && (
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={followups} onChange={(e) => setFollowups(e.target.checked)} />
          {t("Re-review PRs that get new commits")}
        </label>
      )}
      {changed && (
        <div className="row">
          <button
            className="primary sm"
            disabled={save.isPending}
            onClick={() =>
              save.mutate(undefined, {
                onSuccess: (r) => {
                  void client.invalidateQueries({ queryKey: ["automation"] });
                  toast({
                    title: r.applied ? t("Saved") : t("Change proposed"),
                    body: r.applied ? undefined : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
                    icon: r.applied ? "check" : "inbox",
                  });
                },
              })
            }
          >
            {a.can_configure ? t("Save") : t("Propose")}
          </button>
          {!a.can_configure && <span className="small muted">{t("A maintainer applies it.")}</span>}
        </div>
      )}
      <ErrorBox error={save.error} />
    </div>
  );
}
