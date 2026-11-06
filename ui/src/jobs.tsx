// Agents' work, live: every job an agent runs (a review, a fix, a reproduction) as a
// card you can open from anywhere. The open job shows its steps as the agent takes
// them, the questions it waits on, what it produced, and lets the person who asked
// stop it. Updates arrive over the event stream (``job.progress``).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
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
import { Skeleton, useToast } from "./ui";

export const ACTIVE = new Set(["queued", "running", "waiting"]);

const PLAYBOOK: Record<string, { label: string; icon: IconName }> = {
  review: { label: "Review", icon: "pr" },
  followup: { label: "Follow-up review", icon: "pr" },
  fix: { label: "Fix", icon: "sparkles" },
  reproduce: { label: "Reproduction", icon: "server" },
  onboard: { label: "Onboarding", icon: "repo" },
  self_improve: { label: "Self-improvement", icon: "settings" },
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

/** Whether the job waits on the user: a question for them, or a draft for them. */
export function needsMe(job: AgentJob, me: string): boolean {
  return job.questions_for_me > 0 || job.produced.some((p) => p.proposal && p.for === me);
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
        <span className="t">
          {t(pb.label)}
          {job.number != null && (
            <span className="muted">
              {" "}
              · {job.repo}#{job.number}
            </span>
          )}
          {job.title && <span className="work-title"> {job.title}</span>}
        </span>
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
  const bottom = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const steps = job.data?.steps ?? [];
  useEffect(() => {
    const el = body.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) bottom.current?.scrollIntoView({ block: "end" });
  }, [steps.length]);

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
            <div ref={bottom} />
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

function Steps({ steps, active }: { steps: JobStep[]; active: boolean }) {
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
    case "tool":
      return (
        <li className="step">
          <span className={`sdot tool ${result?.ok === false ? "bad" : ""}`} />
          <div className="stack" style={{ gap: 4, minWidth: 0 }}>
            <button className="step-tool" onClick={() => setOpen(!open)} aria-expanded={open}>
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
              <strong>{step.text || t("Tool")}</strong>
              {step.detail && <code className="ellipsis">{step.detail.split("\n")[0]}</code>}
              {result?.ok === false && <span className="pill status-bad">{t("failed")}</span>}
            </button>
            {open && (
              <>
                {step.detail && <pre className="pre">{step.detail}</pre>}
                {result?.detail && <pre className="pre muted">{result.detail}</pre>}
              </>
            )}
          </div>
        </li>
      );
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
  if (Array.isArray(r.refused)) lines.push(<span key="r">{(r.refused as string[]).join("; ")}</span>);
  if (typeof r.findings === "number")
    lines.push(
      <span key="f">
        {r.findings as number} {t("findings")}
        {typeof r.recommendation === "string" ? ` · ${r.recommendation}` : ""}
      </span>,
    );
  if (Array.isArray(r.files)) lines.push(<span key="files">{(r.files as string[]).join(", ")}</span>);
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

