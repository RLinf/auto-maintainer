// Home's working parts: what a first-time user does first, the PRs to review with one
// click (and which agent reviews them), and when reviews start on their own.

import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Automation, type ChatThread, type Me, type PrRow, type RepoRow, type ReviewStart } from "../api";
import { useAgent } from "../agent";
import { agentName, AgentPicker, reviewToast } from "../agentPicker";
import { ErrorBox, prPath, Section, Tier, useAct, When } from "../components";
import { prKey, useSettings } from "../data";
import { useT } from "../i18n";
import { Icon, type IconName } from "../icons";
import { Skeleton, useToast } from "../ui";

// ---- quick actions ----------------------------------------------------------------------------

export function QuickActions() {
  const t = useT();
  const agent = useAgent();
  const navigate = useNavigate();
  const actions: { icon: IconName; title: string; body: string; run: () => void; accent?: boolean }[] = [
    {
      icon: "pr",
      title: t("Review pull requests"),
      body: t("Pick PRs and an agent; drafts come back for you to edit and post."),
      run: () => document.getElementById("review")?.scrollIntoView({ behavior: "smooth" }),
    },
    { icon: "sparkles", title: t("Ask the agent"), body: t("About anything on any page; it sees what you see."), run: () => agent.open(), accent: true },
    { icon: "repo", title: t("Add a repository"), body: t("The agent reads it and drafts how to maintain it."), run: () => navigate("/repos?add=1") },
    { icon: "settings", title: t("Agents and models"), body: t("Choose which agents run and on which models."), run: () => navigate("/settings") },
  ];
  return (
    <div className="quick">
      {actions.map((a) => (
        <button key={a.title} className={`quick-action ${a.accent ? "accent" : ""}`} onClick={a.run}>
          <span className="quick-icon">
            <Icon name={a.icon} size={18} />
          </span>
          <span className="stack" style={{ gap: 2, alignItems: "flex-start", textAlign: "left" }}>
            <strong>{a.title}</strong>
            <span className="small muted" style={{ whiteSpace: "normal" }}>
              {a.body}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}

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
      <span style={{ color: "var(--info)" }}>
        <Icon name="clock" size={12} /> {job.playbook === "followup" ? t("following up…") : t("reviewing…")}
        {job.agent ? ` ${agentName(job.agent)}` : ""}
        {who ? ` · ${who}` : ""}
      </span>
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

export function ReviewCard({ rows, loading, me }: { rows: PrRow[]; loading: boolean; me: Me }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [who, setWho] = useState("");
  const [showAll, setShowAll] = useState(false);
  const sorted = [...rows].sort((a, b) => (b.total ?? -1) - (a.total ?? -1));
  const shown = showAll ? sorted : sorted.slice(0, 8);
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
        <span className="small muted">{rows.length}</span>
        <span className="spacer" />
        <AgentPicker value={who} onChange={setWho} />
        <button className="primary" disabled={!chosen.length || review.isPending} onClick={() => start(chosen)}>
          <Icon name="pr" size={15} />
          {review.isPending ? t("Starting…") : chosen.length ? `${t("Review")} ${chosen.length}` : t("Review selected")}
        </button>
      </div>
      {loading ? (
        <Skeleton rows={4} />
      ) : !rows.length ? (
        <span className="small muted">{t("No PR needs a review.")}</span>
      ) : (
        <div className="list">
          {shown.map((p) => (
            <label key={prKey(p)} className="list-row" style={{ cursor: "pointer" }}>
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
                  {p.repo}#{p.number} · {p.author} · <When at={p.updated_at} />
                  {p.review_state === "FOLLOWUP_REVIEW" || p.review_state === "APPROVED_NEW_COMMITS" ? ` · ${t("updated since the last review")}` : ""}
                </span>
              </span>
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
      {rows.length > 8 && (
        <button className="ghost sm" style={{ marginTop: 8 }} onClick={() => setShowAll(!showAll)}>
          {showAll ? t("Show fewer") : `${t("Show all")} (${rows.length})`}
        </button>
      )}
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

export function AutomationCard({ automations, loading }: { automations: Automation[]; loading: boolean }) {
  const t = useT();
  return (
    <Section title={t("When reviews run")} hint={t("per repository")}>
      <div id="automation" />
      {loading ? (
        <Skeleton rows={3} />
      ) : !automations.length ? (
        <span className="small muted">{t("Add a repository and approve its profile first.")}</span>
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          {automations.map((a) => (
            <AutomationRow key={a.repo} a={a} />
          ))}
        </div>
      )}
    </Section>
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
