// Agents' work, live: every job an agent runs (a review, a fix, a reproduction) as a
// card you can open from anywhere. The open job shows its steps as the agent takes
// them, the questions it waits on, what it produced, and lets the person who asked
// stop it. Updates arrive over the event stream (``job.progress``).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api, type AgentJob, type JobStep } from "./api";
import { agentName } from "./agentPicker";
import { ErrorBox, prPath, useAct, When } from "./components";
import { useMe, useProposals, useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon, type IconName } from "./icons";
import { Markdown } from "./markdown";
import { openJob } from "./openJob";
import { ProposalCard } from "./proposal";
import { QuestionCard } from "./pages/Questions";
import { ReviewTalk } from "./reviewTalk";
import { Skeleton, useFollowBottom, useToast } from "./ui";

export const ACTIVE = new Set(["queued", "running", "waiting"]);

const PLAYBOOK: Record<string, { label: string; icon: IconName }> = {
  review: { label: "Review", icon: "pr" },
  followup: { label: "Follow-up review", icon: "pr" },
  fix: { label: "Fix", icon: "sparkles" },
  reproduce: { label: "Reproduction", icon: "server" },
  onboard: { label: "Onboarding", icon: "repo" },
  self_improve: { label: "Self-improvement", icon: "settings" },
  evaluate: { label: "Priority evaluation", icon: "sort" },
  quality: { label: "Quality score", icon: "sparkles" },
  quick_quality: { label: "Quick quality score", icon: "sparkles" },
};

export function playbookLabel(name: string): string {
  return PLAYBOOK[name]?.label ?? name;
}

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Working",
  waiting: "Waiting",
  done: "Done",
  failed: "Failed",
  cancelled: "Stopped",
};

export function useWork(everyone: boolean) {
  return useQuery({
    queryKey: ["work", everyone],
    queryFn: () => api.get<AgentJob[]>(`/api/work${everyone ? "?everyone=true" : ""}`),
    refetchInterval: (q) => ((q.state.data ?? []).some((j) => ACTIVE.has(j.status)) ? 15_000 : false),
  });
}

/** Everyone's agent jobs on one PR: running ones first, then those of the last week. */
export function usePrWork(repo: string, number: number) {
  return useQuery({
    queryKey: ["work", "pr", repo, number],
    queryFn: () => api.get<AgentJob[]>(`/api/work?everyone=true&repo=${encodeURIComponent(repo)}&number=${number}`),
    refetchInterval: (q) => ((q.state.data ?? []).some((j) => ACTIVE.has(j.status)) ? 15_000 : false),
  });
}

/** Everyone's jobs of one playbook in one repository, from the last day: what the
 * progress of a batch (every PR scored again) is counted from. */
export function useRepoWork(repo: string, playbook: string) {
  return useQuery({
    queryKey: ["work", "repo", repo, playbook],
    queryFn: () => api.get<AgentJob[]>(`/api/work?everyone=true&days=1&repo=${encodeURIComponent(repo)}&playbook=${playbook}`),
    enabled: !!repo,
    refetchInterval: (q) => ((q.state.data ?? []).some((j) => ACTIVE.has(j.status)) ? 15_000 : false),
  });
}

/** The jobs to count progress over: those still running and every job of their
 * batches (one request scores many PRs). With nothing running, the batch that ended
 * last, for ten minutes after it ended. */
export function currentBatch(jobs: AgentJob[], now = Date.now()): AgentJob[] {
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  if (active.length) {
    const batches = new Set(active.map((j) => j.batch).filter(Boolean));
    return jobs.filter((j) => ACTIVE.has(j.status) || (j.batch && batches.has(j.batch)));
  }
  const recent = jobs.filter((j) => now - Date.parse(j.updated_at) < 10 * 60_000);
  if (!recent.length) return [];
  const last = recent.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
  return last.batch ? jobs.filter((j) => j.batch === last.batch) : [last];
}

function batchCounts(jobs: AgentJob[]) {
  return {
    total: jobs.length,
    done: jobs.filter((j) => j.status === "done").length,
    running: jobs.filter((j) => j.status === "running" || j.status === "waiting").length,
    queued: jobs.filter((j) => j.status === "queued").length,
    failed: jobs.filter((j) => j.status === "failed" || j.status === "cancelled").length,
  };
}

/** How far scoring a repository's PRs has got: a bar, the counts, and each agent's job
 * (running ones first), each opening its live view. Nothing when no batch is recent. */
export function ScoringProgress({ repo, me }: { repo: string; me: string }) {
  const t = useT();
  const work = useRepoWork(repo, "evaluate");
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const batch = currentBatch(work.data ?? []);
  if (!batch.length) return null;
  const c = batchCounts(batch);
  const ended = c.done + c.failed;
  const active = c.running + c.queued > 0;
  const order = (j: AgentJob) => (j.status === "running" || j.status === "waiting" ? 0 : j.status === "queued" ? 1 : 2);
  const rows = [...batch].sort((a, b) => order(a) - order(b) || b.updated_at.localeCompare(a.updated_at));
  return (
    <section className="card" style={{ marginBottom: 12 }} aria-label={t("Scoring priority")}>
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <span className={`work-icon ${active ? "live" : ""}`}>
          <Icon name="sort" size={16} />
        </span>
        <strong>{active ? t("Scoring priority") : t("Scoring finished")}</strong>
        <span className="small muted" style={{ fontVariantNumeric: "tabular-nums" }}>
          {ended} / {c.total} {t("scored")}
          {c.running ? ` · ${c.running} ${t("working")}` : ""}
          {c.queued ? ` · ${c.queued} ${t("queued")}` : ""}
          {c.failed ? ` · ${c.failed} ${t("failed")}` : ""}
        </span>
        <span className="spacer" />
        <button className="ghost sm" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? t("Hide the agents' work") : t("Show the agents' work")}
        </button>
      </div>
      <span
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={c.total}
        aria-valuenow={ended}
        style={{ display: "block", height: 6, borderRadius: 3, marginTop: 10, background: "var(--surface-3)", overflow: "hidden" }}
      >
        <span style={{ display: "block", width: `${(100 * ended) / c.total}%`, height: "100%", borderRadius: 3, background: "var(--accent)", transition: "width .4s" }} />
      </span>
      {open && (
        <div className="list" style={{ marginTop: 10 }}>
          {(all ? rows : rows.slice(0, 8)).map((j) => (
            <WorkRow key={j.id} job={j} me={me} />
          ))}
          {rows.length > 8 && (
            <button className="ghost sm" onClick={() => setAll(!all)}>
              {all ? t("Show fewer") : `${t("Show all")} (${rows.length})`}
            </button>
          )}
        </div>
      )}
      <ErrorBox error={work.error} />
    </section>
  );
}

/** One line on Home for a repository's scoring jobs, opening its progress on the Pull
 * requests page; individual scoring jobs would crowd out the other agents' work. */
export function ScoringRow({ repo, jobs }: { repo: string; jobs: AgentJob[] }) {
  const t = useT();
  const navigate = useNavigate();
  const active = jobs.some((j) => ACTIVE.has(j.status));
  const batch = active ? currentBatch(jobs) : jobs;
  const c = batchCounts(batch);
  const latest = batch.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
  return (
    <button className="work-row" onClick={() => navigate(`/prs?repo=${encodeURIComponent(repo)}`)}>
      <span className={`work-icon ${active ? "live" : ""}`}>
        <Icon name="sort" size={16} />
      </span>
      <span className="main">
        <span className="t">
          {t("Priority evaluation")}
          <span className="muted"> · {repo}</span>
        </span>
        <span className="meta">
          {active ? `${c.done + c.failed} / ${c.total} ${t("scored")}` : `${c.done} ${t("scored")}`}
          {c.failed ? ` · ${c.failed} ${t("failed")}` : ""} · <When at={latest.updated_at} />
        </span>
      </span>
      <span className={`pill job-${active ? "running" : "done"}`}>
        {active && <span className="pulse" />}
        {t(active ? "Working" : "Done")}
      </span>
    </button>
  );
}

/** Whether the job waits on the user: a question for them, or a draft for them. */
export function needsMe(job: AgentJob, me: string): boolean {
  return job.questions_for_me > 0 || job.produced.some((p) => p.proposal && p.for === me);
}

/** A job started from somewhere else (a chat answer), as one line that opens its live view. */
export function JobById({ id }: { id: string }) {
  const t = useT();
  const job = useJob(id);
  const me = useMe();
  if (job.data) return <WorkRow job={job.data} me={me.data?.login ?? ""} />;
  return (
    <button className="work-row" onClick={() => openJob(id)}>
      <span className="small muted">{job.isError ? t("This job is no longer kept.") : t("Starting the job…")}</span>
    </button>
  );
}

function useJob(id: string | null) {
  return useQuery({
    queryKey: ["job", id],
    queryFn: () => api.get<AgentJob>(`/api/jobs/${id}`),
    enabled: !!id,
    // A job asked for a moment ago may not exist yet; a running one updates over the
    // event stream, with a slow poll in case the stream drops.
    retry: (n, e) => e instanceof ApiError && e.status === 404 && n < 20,
    retryDelay: 600,
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 10_000 : false),
  });
}

/** One line about a job, for lists. */
export function WorkRow({ job, me }: { job: AgentJob; me: string }) {
  const t = useT();
  const pb = PLAYBOOK[job.playbook] ?? { label: job.playbook, icon: "activity" as IconName };
  const last = job.last_step;
  const active = ACTIVE.has(job.status);
  const waitingOnMe = needsMe(job, me);
  return (
    <button className="work-row" onClick={() => openJob(job.id)}>
      <span className={`work-icon ${active ? "live" : ""}`}>
        <Icon name={pb.icon} size={16} />
      </span>
      <span className="main">
        {job.title ? (
          <>
            <span className="kicker">
              {t(pb.label)}
              {job.number != null && ` · ${job.repo}#${job.number}`}
            </span>
            <span className="t">{job.title}</span>
          </>
        ) : (
          <span className="t">
            {t(pb.label)}
            {job.number != null && (
              <span className="muted">
                {" "}
                · {job.repo}#{job.number}
              </span>
            )}
          </span>
        )}
        <span className="meta">
          {agentName(job.agent) || t("Default agent")}
          {job.by ? ` · ${job.by === me ? t("you") : job.by}` : ""} · <When at={job.updated_at} />
          {active && last ? ` · ${stepLine(last, t)}` : ""}
          {!active && job.error ? ` · ${job.error}` : ""}
        </span>
      </span>
      {waitingOnMe ? (
        <span className="pill status-warn">{job.questions_for_me ? t("asks you") : t("draft for you")}</span>
      ) : (
        <JobStatus job={job} />
      )}
    </button>
  );
}

function JobStatus({ job }: { job: AgentJob }) {
  const t = useT();
  const live = job.status === "running";
  return (
    <span className={`pill job-${job.status}`}>
      {live && <span className="pulse" />}
      {t(STATUS_LABEL[job.status] ?? job.status)}
    </span>
  );
}

function stepLine(step: JobStep, t: (s: string) => string): string {
  switch (step.kind) {
    case "start":
      return `${agentName(step.runtime)} ${t("started")}`;
    case "message":
      return step.text.split("\n")[0].slice(0, 90);
    case "thinking":
      return `${t("Thinking")}: ${step.text.trim().split("\n").slice(-1)[0].slice(0, 80)}`;
    case "tool":
      return `${step.text}${step.detail ? `: ${step.detail.split("\n")[0].slice(0, 70)}` : ""}`;
    case "result":
      return step.ok === false ? t("a command failed") : t("got a result");
    case "question":
      return `${t("asked")}: ${step.text}`;
    case "error":
      return step.text.slice(0, 90);
    case "done":
      return step.ok ? t("finished") : t("failed");
    case "output":
      return `${step.text}: ${step.detail.split("\n").slice(-1)[0].slice(0, 80)}`;
  }
}

/** The live view of a job, in a panel beside the page; ``?job=<id>`` opens it. */
export function JobDrawer() {
  const t = useT();
  const [params, setParams] = useSearchParams();
  const id = params.get("job");
  const close = () =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("job");
        return next;
      },
      { replace: true },
    );
  useEffect(() => {
    if (!id) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  if (!id) return null;
  return (
    <aside className="job-panel open" aria-label={t("Agent's work")}>
      <JobView key={id} id={id} onClose={close} />
    </aside>
  );
}

function JobView({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT();
  const toast = useToast();
  const me = useMe().data;
  const job = useJob(id);
  const questions = useQuestions("pending");
  const proposals = useProposals();
  const stop = useAct(() => api.post<AgentJob>(`/api/jobs/${job.data?.id ?? id}/stop`), [["job"], ["work"], ["prs"]]);
  const body = useRef<HTMLDivElement>(null);
  const steps = job.data?.steps ?? [];
  useFollowBottom(body);

  const d = job.data;
  const pb = d ? (PLAYBOOK[d.playbook] ?? { label: d.playbook, icon: "activity" as IconName }) : null;
  const waiting = (questions.data ?? []).filter((q) => d && q.job_id === d.id);
  const produced = (proposals.data ?? []).filter((p) => d?.produced.some((x) => x.id === p.action_id));
  const active = !!d && ACTIVE.has(d.status);
  return (
    <>
      <div className="agent-head">
        <span className="avatar-ai">
          <Icon name={pb?.icon ?? "activity"} size={16} />
        </span>
        <div className="stack" style={{ gap: 2, minWidth: 0, flex: 1 }}>
          <strong style={{ fontSize: 14.5 }}>
            {pb ? t(pb.label) : t("Agent's work")}
            {d?.number != null && (
              <>
                {" "}
                <Link to={prPath(d.repo ?? "", d.number)} className="small">
                  {d.repo}#{d.number}
                </Link>
              </>
            )}
          </strong>
          {d?.title && <span className="small muted ellipsis">{d.title}</span>}
        </div>
        {d && <JobStatus job={d} />}
        <button className="ghost icon" onClick={onClose} aria-label={t("Close")} title={t("Close")}>
          <Icon name="x" />
        </button>
      </div>
      <div className="agent-body" ref={body}>
        {job.isLoading || (job.error instanceof ApiError && job.error.status === 404 && job.isFetching) ? (
          <div className="stack" style={{ gap: 8 }}>
            <span className="small muted">{t("Starting…")}</span>
            <Skeleton rows={4} />
          </div>
        ) : job.error ? (
          <ErrorBox error={job.error} />
        ) : d ? (
          <>
            <div className="row small muted" style={{ flexWrap: "wrap", gap: 6 }}>
              <span>
                <Icon name="cpu" size={13} /> {agentName(d.agent) || t("Default agent")}
              </span>
              {d.by && (
                <span>
                  · {t("asked by")} {d.by === me?.login ? t("you") : d.by}
                </span>
              )}
              <span>
                · {t("started")} <When at={d.created_at} />
              </span>
              {d.wait && <span>· {d.wait}</span>}
            </div>
            {d.note && (
              <div className="small">
                <span className="muted">{t("Note")}:</span> {d.note}
              </div>
            )}
            {waiting.length > 0 && (
              <section className="stack" style={{ gap: 8 }}>
                <span className="kind-chip warn">{t("The agent is waiting for an answer")}</span>
                {waiting.map((q) => (
                  <QuestionCard key={q.id} question={q} compact />
                ))}
              </section>
            )}
            <Steps steps={steps} active={d.status === "running" || d.status === "queued"} />
            <Outcome job={d} />
            {produced.length > 0 && (
              <section className="stack" style={{ gap: 8 }}>
                <span className="kind-chip">{t("Waiting for your decision")}</span>
                {produced.map((p) => (
                  <ProposalCard key={p.id} proposal={p} />
                ))}
              </section>
            )}
            {(d.playbook === "review" || d.playbook === "followup") && <ReviewTalk job={d} me={me?.login ?? ""} active={active} />}
          </>
        ) : null}
      </div>
      {active && d && (
        <div className="agent-foot">
          <div className="row">
            <span className="small muted">{d.status === "waiting" ? t("Nothing runs while it waits.") : t("The agent is working; you can leave this open or come back later.")}</span>
            <button
              className="ghost danger-text right"
              disabled={stop.isPending}
              onClick={() =>
                stop.mutate(undefined, {
                  onSuccess: () => toast({ title: t("Stopped"), icon: "pause" }),
                })
              }
            >
              <Icon name="pause" size={14} />
              {t("Stop")}
            </button>
          </div>
          <ErrorBox error={stop.error} />
        </div>
      )}
    </>
  );
}

export function Steps({ steps, active }: { steps: JobStep[]; active: boolean }) {
  const t = useT();
  if (!steps.length)
    return <div className="small muted">{active ? t("The agent has not started yet.") : t("This job ran no agent, or its steps were not kept.")}</div>;
  const items: ReactNode[] = [];
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    const next = steps[i + 1];
    if (s.kind === "result") continue;
    const result = s.kind === "tool" && next?.kind === "result" ? next : null;
    items.push(<Step key={i} step={s} result={result} />);
  }
  return (
    <ol className="steps-live">
      {items}
      {active && (
        <li className="step step-working">
          <span className="sdot live" />
          <span className="small muted">{t("Working…")}</span>
        </li>
      )}
    </ol>
  );
}

function Step({ step, result }: { step: JobStep; result: JobStep | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  switch (step.kind) {
    case "start":
      return (
        <li className="step">
          <span className="sdot" />
          <span className="small">
            <strong>{agentName(step.runtime)}</strong> {t("started")}
            {step.task_kind && !["review", "followup", "fix", "reproduce"].includes(step.task_kind) ? ` (${step.task_kind})` : ""}
            <span className="muted">
              {" "}
              · <When at={step.at} />
            </span>
          </span>
        </li>
      );
    case "message":
      return (
        <li className="step">
          <span className="sdot msg" />
          <Markdown text={step.text} className="step-msg" />
        </li>
      );
    case "thinking":
      return (
        <li className="step">
          <span className="sdot think" />
          <div className="stack" style={{ gap: 4, minWidth: 0 }}>
            <button className="step-tool" onClick={() => setOpen(!open)} aria-expanded={open}>
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
              <span className="muted">{t("Thinking")}</span>
              {!open && <span className="ellipsis muted small">{step.text.trim().split("\n").slice(-1)[0]}</span>}
            </button>
            {open && <div className="step-think">{step.text.trim()}</div>}
          </div>
        </li>
      );
    case "tool": {
      // Some CLIs title a tool call with the whole command, several lines long: the
      // line shows its first line, and the full text when opened.
      const title = (step.text || t("Tool")).trim().split("\n")[0];
      const shown = step.detail?.trim().split("\n")[0] ?? "";
      const full = step.detail || (step.text.includes("\n") ? step.text : "");
      return (
        <li className="step">
          <span className={`sdot tool ${result?.ok === false ? "bad" : ""}`} />
          <div className="stack" style={{ gap: 4, minWidth: 0 }}>
            <button className="step-tool" onClick={() => setOpen(!open)} aria-expanded={open} title={title}>
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
              <strong className="ellipsis">{title}</strong>
              {shown && !step.text.includes("\n") && !title.includes(shown.slice(0, 40)) && <code className="ellipsis">{shown}</code>}
              {result?.ok === false && <span className="pill status-bad">{t("failed")}</span>}
            </button>
            {open && (
              <>
                {full && <pre className="pre">{full}</pre>}
                {result?.detail && <pre className="pre muted">{result.detail}</pre>}
              </>
            )}
          </div>
        </li>
      );
    }
    case "output": {
      // The live output of a run on a test machine: its newest line, and when opened
      // its newest lines, so a long setup shows it is moving.
      const lines = step.detail.split("\n");
      return (
        <li className="step">
          <span className="sdot tool" />
          <div className="stack" style={{ gap: 4, minWidth: 0 }}>
            <button className="step-tool" onClick={() => setOpen(!open)} aria-expanded={open} title={lines[lines.length - 1]}>
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
              <Icon name="server" size={12} />
              <strong className="ellipsis">{step.text}</strong>
              {!open && <code className="ellipsis">{lines[lines.length - 1]}</code>}
            </button>
            {open && <pre className="pre lab-output">{step.detail}</pre>}
          </div>
        </li>
      );
    }
    case "question":
      return (
        <li className="step">
          <span className="sdot ask" />
          <span className="small">
            {t("Asked")}: <strong>{step.text}</strong>{" "}
            <span className="muted">
              {step.ok === true ? t("allowed") : step.ok === false ? t("denied") : ""}
              {step.detail === "automatic" ? ` (${t("automatically")})` : ""}
            </span>
          </span>
        </li>
      );
    case "error":
      return (
        <li className="step">
          <span className="sdot bad" />
          <span className="small" style={{ color: "var(--bad)" }}>
            {step.text}
          </span>
        </li>
      );
    case "done":
      return (
        <li className="step">
          <span className={`sdot ${step.ok ? "ok" : "bad"}`} />
          <span className="small">
            {agentName(step.runtime)} {step.ok ? t("finished") : `${t("failed")}: ${step.text}`}
          </span>
        </li>
      );
    default:
      return null;
  }
}

/** What the job came to, in words: findings, files, a reproduction's runs, or why it
 * stopped. */
function Outcome({ job }: { job: AgentJob }) {
  const t = useT();
  const r = job.result ?? {};
  if (job.status === "failed" || job.status === "cancelled")
    return (
      <div className="outcome bad">
        <Icon name="x" size={14} /> {job.error || t("Failed")}
      </div>
    );
  if (job.status !== "done") return null;
  const lines: ReactNode[] = [];
  if (typeof r.skipped === "string") lines.push(<span key="s">{r.skipped}</span>);
  if (job.playbook === "evaluate" && typeof r.tier === "string")
    lines.push(
      <span key="tier">
        <strong>{r.tier as string}</strong> · {Number(r.total).toFixed(1)}
        {r.scored === false ? <span className="muted"> · {t("the scores were kept; only the total was computed again")}</span> : null}
      </span>,
    );
  if (Array.isArray(r.refused)) lines.push(<span key="r">{(r.refused as string[]).join("; ")}</span>);
  if (typeof r.findings === "number")
    lines.push(
      <span key="f">
        {r.findings as number} {t("findings")}
        {typeof r.recommendation === "string" ? ` · ${r.recommendation}` : ""}
      </span>,
    );
  if (Array.isArray(r.files)) lines.push(<span key="files">{(r.files as string[]).join(", ")}</span>);
  if (Array.isArray(r.labels) && r.labels.length)
    lines.push(
      <span key="labels">
        {t("Suggested labels")}: {(r.labels as string[]).join(", ")}
      </span>,
    );
  if (typeof r.conclusion === "string") {
    const runs = (r.runs ?? {}) as Record<string, { exit_code?: number; timed_out?: boolean; duration_seconds?: number; error?: string }>;
    lines.push(
      <span key="c">
        <strong>{t(CONCLUSION[r.conclusion as string] ?? (r.conclusion as string))}</strong>
        {Object.entries(runs).map(([side, run]) => (
          <span key={side} className="muted">
            {" "}
            · {side === "base" ? t("before the PR") : t("at the PR's head")}: {run.error ? run.error : run.timed_out ? t("timed out") : `${t("exit code")} ${run.exit_code}`}
            {run.duration_seconds != null ? ` (${run.duration_seconds}s)` : ""}
          </span>
        ))}
      </span>,
    );
  }
  if (typeof r.notes === "string" && r.notes) lines.push(<span key="n" className="muted">{r.notes as string}</span>);
  if (!lines.length) lines.push(<span key="d">{t("Finished")}</span>);
  return (
    <div className="outcome ok">
      <Icon name="check" size={14} />
      <span className="stack" style={{ gap: 2 }}>
        {lines}
      </span>
    </div>
  );
}

const CONCLUSION: Record<string, string> = {
  reproduced: "Reproduced",
  not_reproduced: "Not reproduced",
  inconclusive: "Inconclusive",
};


/** What agents do and have drafted on one PR, on that PR's page: every job on it
 * (anyone's), each opening its live view, and every draft waiting for a decision,
 * decidable in place. Nothing shows when there is nothing. */
export function PrWork({ repo, number, me }: { repo: string; number: number; me: string }) {
  const t = useT();
  const work = usePrWork(repo, number);
  const proposals = useProposals();
  const [all, setAll] = useState(false);
  const jobs = work.data ?? [];
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  const ended = jobs.filter((j) => !ACTIVE.has(j.status));
  const drafts = (proposals.data ?? []).filter((p) => p.action?.action.repo === repo && p.action?.action.number === number);
  if (!jobs.length && !drafts.length) return null;
  const shownEnded = all ? ended : ended.slice(0, 3);
  return (
    <section className="card pr-work">
      <div className="row" style={{ marginBottom: 6 }}>
        <h2>{t("On this PR")}</h2>
        {active.length > 0 && (
          <span className="small muted">
            <span className="pulse" /> {active.length} {t(active.length === 1 ? "agent working" : "agents working")}
          </span>
        )}
        {drafts.length > 0 && (
          <span className="small muted">
            · {drafts.length} {t(drafts.length === 1 ? "draft waits for a decision" : "drafts wait for a decision")}
          </span>
        )}
      </div>
      {jobs.length > 0 && (
        <div className="list">
          {[...active, ...shownEnded].map((j) => (
            <WorkRow key={j.id} job={j} me={me} />
          ))}
        </div>
      )}
      {ended.length > 3 && (
        <button className="ghost sm" onClick={() => setAll(!all)}>
          {all ? t("Show fewer") : `${t("Show all")} (${jobs.length})`}
        </button>
      )}
      {drafts.length > 0 && (
        <div className="stack" style={{ gap: 10, marginTop: jobs.length ? 12 : 0 }}>
          <span className="kind-chip">{t("Waiting for a decision")}</span>
          {drafts.map((p) => (
            <ProposalCard key={p.id} proposal={p} compact />
          ))}
        </div>
      )}
      <ErrorBox error={work.error} />
    </section>
  );
}
