// Merge scheduling (design 5.5): maintainers choose which ready PRs merge and in what
// order; the service gets each one's CI going the way the repository starts it, watches
// it, reruns what flakes, sends what really fails to the failure lane with its root
// cause, and merges what passes. Everyone who sees the repository sees the page;
// arranging merges needs a maintainer, and the settings go through the configuration
// gate (a non-maintainer's change becomes a proposal).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Empty, ErrorBox, PageHead, prPath, Section, useAct, When } from "../components";
import { useRepos } from "../data";
import { FixCiButton } from "../fixCi";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { Markdown } from "../markdown";
import { openJob } from "../openJob";
import { RepoTabs, useRepoScope } from "../repoScope";
import { Skeleton, useToast } from "../ui";

type Phase = "queued" | "updating" | "starting" | "running" | "rerunning" | "diagnosing" | "approval" | "merging" | "failed" | "held" | "merged";

interface CiSummary {
  state: string;
  passed: number;
  running: number;
  failed: string[];
}

interface Row {
  number: number;
  title: string;
  author: string;
  url: string;
  updated_at: string | null;
  behind: boolean;
  ci: CiSummary;
}

interface Scheduled extends Row {
  phase: Phase;
  detail: string;
  since: string | null;
  scheduled_by: string;
  auto: boolean;
  failed_jobs: string[];
  causes: string[];
  finding: string;
  approval_needed: boolean;
  stale: boolean;
  job: { id: string; status: string; playbook: string } | null;
  forced: boolean;
}

interface NotReady extends Row {
  reasons: string[];
  conflict: boolean;
}

interface CiStart {
  how: string;
  gate_jobs: string[];
  summary_jobs: string[];
  start_minutes: number;
  run_minutes: number;
  gate_label: string | null;
  reruns: number;
  optional_checks: string[];
}

interface Draft {
  draft: {
    how: string;
    gate_label: string;
    gate_jobs: string[];
    summary_jobs: string[];
    start_minutes: number;
    run_minutes: number;
    merge_notes: string;
    approvals: number;
    ready: string[];
    optional_checks: string[];
    blocking_roles: string[];
    diagnose: "agent" | "patterns";
    max_reruns: number;
    backoff_seconds: number;
    rerun_unknown: number;
    root_cause: boolean;
    flake_patterns: Pattern[];
    merge_method: string;
    subject: string;
    preserve_trailers: string[];
    update_branch: boolean;
    parallel: number;
    required_workflows: string[];
    cancelled_is_failure: boolean;
  };
  dropped: string[];
  by: string;
  at: string;
  source?: "words" | "investigation";
  words?: string;
}

interface Pattern {
  id: string;
  pattern: string;
  category: "flaky" | "infra";
  description: string;
  own?: boolean;
}

interface Flaky {
  diagnose: "agent" | "patterns";
  max_reruns: number;
  backoff_seconds: number;
  rerun_unknown: number;
  root_cause: boolean;
  patterns: Pattern[];
  suggestions: (Pattern & { from: string; by: string; at: string })[];
}

interface MergeView {
  repo: string;
  enabled: boolean;
  can_arrange: boolean;
  signed_in: boolean;
  mode: "serial" | "parallel";
  parallel: number;
  auto: boolean;
  auto_by: string | null;
  paused: boolean;
  checking: boolean;
  rules: {
    approvals: number;
    code_owners: boolean | null;
    last_push: boolean | null;
    threads: boolean;
    up_to_date: boolean | null;
    update_branch: boolean;
    required_checks: string[] | null;
    required_workflows: string[];
    methods: string[] | null;
    method: string;
    fork_approval: string | null;
    blocking_labels: string[];
    notes: string;
    hygiene: string[];
    ready: string[];
    ready_set: boolean;
    blocking_roles: string[];
    label_roles: Record<string, string>;
    subject: string;
    preserve_trailers: string[];
    cancelled_is_failure: boolean;
  };
  ci_start: CiStart;
  flaky: Flaky;
  draft: Draft | null;
  rules_job: { id: string; status: string } | null;
  scheduled: Scheduled[];
  failed: Scheduled[];
  candidates: Row[];
  not_ready: NotReady[];
  merged: { number: number; title: string; at: string; since: string | null }[];
}

interface Saved {
  applied: boolean;
  reason: string | null;
}

const PHASE: Record<Phase, { label: string; cls: string }> = {
  queued: { label: "Waiting for its turn", cls: "job-waiting" },
  updating: { label: "Merging the base in", cls: "job-running" },
  starting: { label: "Starting CI", cls: "job-running" },
  running: { label: "CI running", cls: "job-running" },
  rerunning: { label: "Rerunning a flake", cls: "job-running" },
  diagnosing: { label: "Diagnosing failures", cls: "job-running" },
  approval: { label: "CI passed, waiting to meet the merge rules", cls: "status-warn" },
  merging: { label: "Merging", cls: "job-running" },
  failed: { label: "Failed", cls: "status-bad" },
  held: { label: "Held back", cls: "status-warn" },
  merged: { label: "Merged", cls: "job-done" },
};

const FORK_APPROVAL: Record<string, string> = {
  first_time_contributors_new_to_github: "first-time contributors new to GitHub",
  first_time_contributors: "first-time contributors",
  all_external_contributors: "every outside contributor",
};

export default function MergePage() {
  const t = useT();
  const repos = useRepos();
  const names = (repos.data ?? []).map((r) => r.repo).sort((a, b) => a.localeCompare(b));
  const [scope, setScope] = useRepoScope(names);
  const toast = useToast();
  const q = useQuery({
    queryKey: ["merge-schedule", scope],
    queryFn: () => api.get<MergeView>(`/api/merge-schedule/${scope}/view`),
    enabled: !!scope,
    // Faster while every PR is being read again, so the lists follow along.
    refetchInterval: (query) => (query.state.data?.checking ? 4_000 : 20_000),
  });
  const v = q.data;
  const check = useAct(() => api.post<{ started: boolean; prs: number }>(`/api/merge-schedule/${scope}/check`), [["merge-schedule"]]);
  const checking = check.isPending || !!v?.checking;
  return (
    <>
      <PageHead
        title={t("Merge scheduling")}
        sub={t("Choose which ready PRs merge and in what order. The service starts their CI the way the repository starts it, reruns what flakes, sets real failures aside with their cause, and merges what passes.")}
        actions={
          v?.enabled && (
            <button
              disabled={checking}
              title={t("Read every open PR from GitHub again now, instead of at the next poll")}
              onClick={() =>
                check.mutate(undefined, {
                  onSuccess: (out) =>
                    toast({
                      title: out.started ? `${t("Checking")} ${out.prs} ${t("PRs")}` : t("Already checking"),
                      body: t("Each PR is read from GitHub again; the lists update as they are read."),
                      icon: "refresh",
                    }),
                  onError: (e) => toast({ title: t("Not started"), body: String((e as Error).message), icon: "x" }),
                })
              }
            >
              <Icon name="refresh" size={14} />
              {checking ? t("Checking all PRs…") : t("Check all PRs")}
            </button>
          )
        }
      />
      <RepoTabs names={names} scope={scope} onPick={setScope} />
      {repos.isLoading || (q.isLoading && !!scope) ? (
        <Skeleton rows={6} />
      ) : !scope ? (
        <Empty icon="merge">{t("No repository here yet.")}</Empty>
      ) : !v ? (
        <ErrorBox error={q.error} />
      ) : !v.enabled ? (
        <TurnOn v={v} />
      ) : (
        <div className="stack" style={{ gap: 18 }}>
          <Controls v={v} />
          <ScheduleSection v={v} />
          {v.failed.length > 0 && <FailureLane v={v} />}
          <Candidates v={v} />
          <NotReadyList v={v} />
          {v.draft && <DraftRules v={v} draft={v.draft} />}
          <div className="merge-grid">
            <RulesCard v={v} />
            <CiStartCard v={v} />
          </div>
          <FlakyCard v={v} />
          {v.merged.length > 0 && <MergedList v={v} />}
        </div>
      )}
    </>
  );
}

function useSaveSettings(repo: string) {
  const t = useT();
  const toast = useToast();
  const save = useAct((body: Record<string, unknown>) => api.put<Saved>(`/api/merge-schedule/${repo}/settings`, body), [["merge-schedule"]]);
  return (body: Record<string, unknown>) =>
    save.mutate(body, {
      onSuccess: (out) =>
        toast({
          title: out.applied ? t("Saved") : t("Proposed to the maintainers"),
          body: out.applied ? undefined : out.reason || undefined,
          icon: out.applied ? "check" : "inbox",
        }),
      onError: (e) => toast({ title: t("Not saved"), body: String((e as Error).message ?? e), icon: "x" }),
    });
}

function TurnOn({ v }: { v: MergeView }) {
  const t = useT();
  const save = useSaveSettings(v.repo);
  return (
    <Section title={t("Merge scheduling is off for this repository")}>
      <div className="stack" style={{ gap: 10 }}>
        <span className="small muted">
          {t("Turned on, the PRs a maintainer schedules here merge through the service: it runs their CI and merges the ones that pass. Nothing merges that nobody scheduled.")}
        </span>
        <div>
          <button className="primary" onClick={() => save({ enabled: true })}>
            <Icon name="merge" size={14} />
            {v.can_arrange ? t("Turn on merge scheduling") : t("Propose turning it on")}
          </button>
        </div>
      </div>
    </Section>
  );
}

function Controls({ v }: { v: MergeView }) {
  const t = useT();
  const save = useSaveSettings(v.repo);
  const [parallel, setParallel] = useState(String(v.parallel));
  useEffect(() => setParallel(String(v.parallel)), [v.parallel]);
  return (
    <section className="card merge-controls">
      <div className="merge-control">
        <span className="small muted">{t("How they run")}</span>
        {v.mode === "serial" ? (
          <strong className="small">{t("One at a time: each merges the base in and runs its CI after the one before it merged, since PRs must be up to date with the base.")}</strong>
        ) : (
          <span className="row small" style={{ gap: 6, flexWrap: "wrap" }}>
            <strong>{t("Several at once, up to")}</strong>
            <input
              className="merge-number"
              value={parallel}
              inputMode="numeric"
              disabled={!v.can_arrange}
              aria-label={t("How many run their CI at once")}
              onChange={(e) => setParallel(e.target.value.replace(/\D/g, ""))}
              onBlur={() => {
                const n = Math.min(20, Math.max(1, Number(parallel) || v.parallel));
                if (n !== v.parallel) save({ parallel: n });
                setParallel(String(n));
              }}
            />
            <span className="muted">{t("PRs run their CI at the same time.")}</span>
          </span>
        )}
      </div>
      <label className="merge-control merge-auto">
        <input type="checkbox" checked={v.auto} disabled={!v.can_arrange} onChange={(e) => save({ auto: e.target.checked })} />
        <span className="stack" style={{ gap: 2 }}>
          <strong className="small">{t("Schedule ready PRs automatically")}</strong>
          <span className="small muted">
            {v.auto
              ? `${t("Every PR that becomes ready is scheduled as")} ${v.auto_by ?? "?"}. ${t("A fork's held workflows still wait for a person to approve them.")}`
              : t("Off: ready PRs wait below until a maintainer schedules them.")}
          </span>
        </span>
      </label>
      {v.paused && (
        <span className="pill status-warn" style={{ alignSelf: "center" }}>
          {t("Paused: nothing is merged until the pause ends")}
        </span>
      )}
    </section>
  );
}

/** Open the live view of the agent's work on a PR: its steps, the commands and
 * files it used, its output. Shown while an agent works, or when it is diagnosing. */
function WatchButton({ job, busy }: { job: Scheduled["job"]; busy: boolean }) {
  const t = useT();
  if (!job || (!busy && job.status !== "running")) return null;
  return (
    <button className="sm" title={t("See what the agent is doing")} onClick={() => openJob(job.id)}>
      <Icon name="eye" size={13} />
      {t("Watch")}
    </button>
  );
}

function PrLink({ repo, row }: { repo: string; row: Row }) {
  return (
    <Link to={prPath(repo, row.number)} className="merge-pr">
      <span className="mono muted">#{row.number}</span> <span className="merge-title">{row.title}</span>
    </Link>
  );
}

function CiLine({ ci }: { ci: CiSummary }) {
  const t = useT();
  const text =
    ci.state === "gated"
      ? t("CI not started (its label is missing)")
      : ci.state === "approval"
        ? t("CI waits for a maintainer's approval")
        : ci.state === "running"
          ? `${ci.running} ${t("running")}, ${ci.passed} ${t("passed")}`
          : ci.state === "failed"
            ? `${t("Failed")}: ${ci.failed.join(", ") || t("cancelled")}`
            : ci.state === "passed"
              ? t("CI passed")
              : ci.state === "stopped"
                ? t("CI stopped at its gate")
                : t("No CI yet");
  const tone = ci.state === "failed" ? "var(--bad)" : ci.state === "passed" ? "var(--ok)" : "var(--muted)";
  return (
    <span className="small" style={{ color: tone }}>
      <Icon name="activity" size={12} /> {text}
    </span>
  );
}

function ScheduleSection({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const order = useAct((numbers: number[]) => api.put(`/api/merge-schedule/${v.repo}/order`, { numbers }), [["merge-schedule"]]);
  const remove = useAct((n: number) => api.del(`/api/merge-schedule/${v.repo}/${n}`), [["merge-schedule"]]);
  const move = (i: number, by: number) => {
    const numbers = v.scheduled.map((s) => s.number);
    const j = i + by;
    if (j < 0 || j >= numbers.length) return;
    [numbers[i], numbers[j]] = [numbers[j], numbers[i]];
    order.mutate([...numbers, ...v.failed.map((f) => f.number)], { onError: (e) => toast({ title: t("Not moved"), body: String((e as Error).message), icon: "x" }) });
  };
  return (
    <Section title={t("Schedule")} hint={`${v.scheduled.length}`}>
      {v.scheduled.length === 0 ? (
        <Empty icon="merge">{v.candidates.length ? t("Nothing scheduled. Schedule a ready PR below.") : t("Nothing scheduled.")}</Empty>
      ) : (
        <ol className="list merge-list">
          {v.scheduled.map((s, i) => {
            const p = PHASE[s.phase] ?? PHASE.queued;
            return (
              <li key={s.number} className="list-row merge-row">
                <span className="merge-pos mono">{i + 1}</span>
                <div className="stack merge-main">
                  <PrLink repo={v.repo} row={s} />
                  <span className="row merge-meta">
                    <span className={`pill ${p.cls}`}>{t(p.label)}</span>
                    {s.detail && <span className="small muted clamp1">{t(s.detail)}</span>}
                    {s.since && (
                      <span className="small muted">
                        <When at={s.since} />
                      </span>
                    )}
                  </span>
                  <span className="row merge-meta">
                    <CiLine ci={s.ci} />
                    <span className="small muted">
                      {t("scheduled by")} {s.scheduled_by}
                      {s.auto && ` (${t("automatically")})`}
                      {s.forced && ` (${t("by hand, before it was ready")})`}
                    </span>
                  </span>
                  {s.approval_needed && (
                    <span className="small" style={{ color: "var(--warn)" }}>
                      {t("GitHub holds this fork's workflows until a person approves running them:")} <Link to={prPath(v.repo, s.number)}>{t("Run CI on the PR page")}</Link>
                    </span>
                  )}
                  {s.finding && <span className="small merge-finding">{s.finding}</span>}
                </div>
                <WatchButton job={s.job} busy={s.phase === "diagnosing"} />
                {v.can_arrange && (
                  <div className="row merge-actions">
                    <button className="sm ghost" aria-label={t("Move up")} title={t("Move up")} disabled={i === 0 || order.isPending} onClick={() => move(i, -1)}>
                      ↑
                    </button>
                    <button className="sm ghost" aria-label={t("Move down")} title={t("Move down")} disabled={i === v.scheduled.length - 1 || order.isPending} onClick={() => move(i, 1)}>
                      ↓
                    </button>
                    <button className="sm" disabled={remove.isPending} onClick={() => remove.mutate(s.number)}>
                      {t("Unschedule")}
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
      <ErrorBox error={order.error || remove.error} />
    </Section>
  );
}

function FailureLane({ v }: { v: MergeView }) {
  const t = useT();
  const retry = useAct((n: number) => api.post(`/api/merge-schedule/${v.repo}/${n}/retry`), [["merge-schedule"]]);
  const flaky = useAct((n: number) => api.post(`/api/merge-schedule/${v.repo}/${n}/flaky`), [["merge-schedule"]]);
  const remove = useAct((n: number) => api.del(`/api/merge-schedule/${v.repo}/${n}`), [["merge-schedule"]]);
  return (
    <Section title={t("Failure lane")} hint={t("Set aside after their CI really failed; the next PR took the turn.")}>
      <div className="list">
        {v.failed.map((f) => (
          <div key={f.number} className="list-row merge-row">
            <span className="merge-pos">
              <Icon name="x" size={14} />
            </span>
            <div className="stack merge-main">
              <PrLink repo={v.repo} row={f} />
              <span className="small" style={{ color: "var(--bad)" }}>
                {f.detail}
                {f.since && (
                  <span className="muted">
                    {" · "}
                    <When at={f.since} />
                  </span>
                )}
              </span>
              {f.causes.length > 0 && <Markdown className="md merge-causes" text={f.causes.join("\n")} />}
              {f.finding && <span className="small merge-finding">{f.finding}</span>}
            </div>
            <WatchButton job={f.job} busy={false} />
            {v.can_arrange && (
              <div className="row merge-actions">
                <FixCiButton repo={v.repo} number={f.number} failed={f.failed_jobs.length ? f.failed_jobs : f.ci.failed} canPush={v.signed_in} size="sm" />
                <button className="sm" disabled={retry.isPending} onClick={() => retry.mutate(f.number)}>
                  {t("Retry")}
                </button>
                <button
                  className="sm"
                  disabled={flaky.isPending || !f.failed_jobs.length}
                  title={t("Mark these failures flaky, so the same failure is rerun from now on, and run them again")}
                  onClick={() => flaky.mutate(f.number)}
                >
                  {t("Flaky, rerun")}
                </button>
                <button className="sm ghost" disabled={remove.isPending} onClick={() => remove.mutate(f.number)}>
                  {t("Unschedule")}
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
      <ErrorBox error={retry.error || remove.error || flaky.error} />
    </Section>
  );
}

function Candidates({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const schedule = useAct((n: number) => api.post(`/api/merge-schedule/${v.repo}/schedule`, { number: n }), [["merge-schedule"]]);
  const all = useAct(async () => {
    for (const c of v.candidates) await api.post(`/api/merge-schedule/${v.repo}/schedule`, { number: c.number });
  }, [["merge-schedule"]]);
  return (
    <Section
      title={t("Ready to schedule")}
      hint={v.auto ? t("Automatic scheduling adds these as they become ready.") : t("They meet the conditions to schedule. Scheduled, a PR merges the base in, runs its CI, and merges.")}
      aside={
        v.can_arrange &&
        v.candidates.length > 1 && (
          <button className="sm" disabled={all.isPending} onClick={() => all.mutate(undefined)}>
            {t("Schedule all")}
          </button>
        )
      }
    >
      {v.candidates.length === 0 ? (
        <Empty icon="check">{t("No PR meets the conditions to schedule right now.")}</Empty>
      ) : (
        <div className="list">
          {v.candidates.map((c) => (
            <div key={c.number} className="list-row merge-row">
              <div className="stack merge-main">
                <PrLink repo={v.repo} row={c} />
                <span className="row merge-meta">
                  <span className="small muted">{c.author}</span>
                  <CiLine ci={c.ci} />
                  {c.behind && v.mode === "serial" && <span className="small muted">{t("behind the base: it merges it in on its turn")}</span>}
                </span>
              </div>
              {v.can_arrange && (
                <button
                  className="primary sm"
                  disabled={schedule.isPending}
                  onClick={() =>
                    schedule.mutate(c.number, {
                      onSuccess: () => toast({ title: `${t("Scheduled")} #${c.number}`, icon: "merge" }),
                      onError: (e) => toast({ title: t("Not scheduled"), body: String((e as Error).message), icon: "x" }),
                    })
                  }
                >
                  {t("Schedule")}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <ErrorBox error={all.error} />
    </Section>
  );
}

function NotReadyList({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const force = useAct((n: number) => api.post(`/api/merge-schedule/${v.repo}/schedule`, { number: n, force: true }), [["merge-schedule"]]);
  if (!v.not_ready.length) return null;
  return (
    <details className="card merge-details">
      <summary>
        <strong>{t("Not ready yet")}</strong> <span className="muted small">({v.not_ready.length})</span>
      </summary>
      <div className="list">
        {v.not_ready.map((r) => (
          <div key={r.number} className="list-row merge-row">
            <div className="stack merge-main">
              <PrLink repo={v.repo} row={r} />
              <span className="small muted">{r.reasons.map((x) => t(x)).join(" · ")}</span>
            </div>
            {v.can_arrange && (
              <button
                className="sm ghost"
                disabled={force.isPending}
                title={t("Run its CI on its turn though it is not ready; it merges once the merge rules hold")}
                onClick={() =>
                  force.mutate(r.number, {
                    onSuccess: () => toast({ title: `${t("Scheduled by hand")} #${r.number}`, icon: "merge" }),
                    onError: (e) => toast({ title: t("Not scheduled"), body: String((e as Error).message), icon: "x" }),
                  })
                }
              >
                {t("Schedule anyway")}
              </button>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}

function Yes({ on, children }: { on: boolean | null; children: React.ReactNode }) {
  if (!on) return null;
  return (
    <li className="small">
      <Icon name="check" size={12} /> {children}
    </li>
  );
}

const READY = ["approvals", "threads_resolved", "no_changes_requested", "not_draft", "no_blocking_labels", "no_conflict", "hygiene"] as const;

function conditionText(c: string, r: MergeView["rules"], t: (s: string) => string): React.ReactNode {
  switch (c) {
    case "approvals":
      return `${r.approvals} ${t("approvals from people with write access")}${r.code_owners ? `, ${t("a code owner's among them")}` : ""}${r.last_push ? `; ${t("the last push approved by someone else")}` : ""}`;
    case "threads_resolved":
      return t("Every review conversation is resolved");
    case "no_changes_requested":
      return t("No reviewer requested changes");
    case "not_draft":
      return t("Not a draft");
    case "no_blocking_labels":
      return `${t("None of the labels")} ${r.blocking_labels.join(", ") || "—"}`;
    case "no_conflict":
      return t("No conflict with the base");
    case "hygiene":
      return (
        <>
          {t("The repository's checks pass:")} <span className="mono">{r.hygiene.join(", ") || "—"}</span>
        </>
      );
    default:
      return c;
  }
}

function RulesCard({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const r = v.rules;
  const [editing, setEditing] = useState(false);
  const [editingLabels, setEditingLabels] = useState(false);
  const [picked, setPicked] = useState<string[]>(r.ready);
  useEffect(() => setPicked(r.ready), [r.ready.join(",")]);
  const save = useAct((ready: string[] | null) => api.put<Saved>(`/api/merge-schedule/${v.repo}/ready`, { ready }), [["merge-schedule"]]);
  const done = (out: Saved) => {
    toast({ title: out.applied ? t("Saved") : t("Proposed to the maintainers"), icon: out.applied ? "check" : "inbox" });
    setEditing(false);
  };
  const optional = new Set(v.ci_start.optional_checks.map((c) => c.toLowerCase()));
  const checks = [...(r.required_checks ?? []).filter((c) => !optional.has(c.toLowerCase())), ...r.required_workflows.map((w) => `${w} (${t("workflow")})`)];
  const after = READY.filter((c) => !r.ready.includes(c) && (c !== "hygiene" || r.hygiene.length) && (c !== "no_blocking_labels" || r.blocking_labels.length));
  return (
    <Section title={t("Conditions")} hint={t("Read from GitHub's branch rules and the repository's profile")}>
      <div className="stack" style={{ gap: 14 }}>
        {v.rules_job && (
          <span className="row small merge-finding" style={{ gap: 8 }}>
            <Icon name="sparkles" size={13} />
            {t("An agent is drafting the rules.")}
            <button className="sm right" onClick={() => openJob(v.rules_job!.id)}>
              <Icon name="eye" size={13} />
              {t("Watch")}
            </button>
          </span>
        )}
        {v.can_arrange && <DescribeBox v={v} />}
        <div className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ gap: 8 }}>
            <strong className="small">{t("Schedulable when")}</strong>
            {!r.ready_set && <span className="small muted">({t("every condition of the merge rules")})</span>}
            {v.can_arrange && !editing && (
              <button className="sm ghost right" onClick={() => setEditing(true)}>
                {t("Edit")}
              </button>
            )}
          </div>
          {editing ? (
            <div className="stack" style={{ gap: 6 }}>
              {READY.map((c) => (
                <label key={c} className="row small" style={{ gap: 8 }}>
                  <input type="checkbox" checked={picked.includes(c)} onChange={(e) => setPicked(e.target.checked ? [...picked, c] : picked.filter((x) => x !== c))} />
                  <span>{conditionText(c, r, t)}</span>
                </label>
              ))}
              <div className="row" style={{ gap: 8 }}>
                <button className="sm ghost" disabled={save.isPending} onClick={() => save.mutate(null, { onSuccess: done })}>
                  {t("Use the merge rules")}
                </button>
                <button className="sm ghost right" onClick={() => setEditing(false)}>
                  {t("Cancel")}
                </button>
                <button className="sm primary" disabled={save.isPending} onClick={() => save.mutate(READY.filter((c) => picked.includes(c)), { onSuccess: done })}>
                  {t("Save")}
                </button>
              </div>
              <ErrorBox error={save.error} />
            </div>
          ) : (
            <ul className="merge-rules">
              {r.ready.length === 0 && <li className="small muted">{t("Every open PR")}</li>}
              {r.ready.map((c) => (
                <Yes key={c} on>
                  {conditionText(c, r, t)}
                </Yes>
              ))}
              <li className="small muted">{t("It need not be up to date with the base: the service merges the base in before its CI runs.")}</li>
            </ul>
          )}
        </div>
        <div className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ gap: 8 }}>
            <strong className="small">{t("Merges after its CI when")}</strong>
            {v.can_arrange && !editingLabels && (
              <button className="sm ghost right" onClick={() => setEditingLabels(true)}>
                {t("Edit")}
              </button>
            )}
          </div>
          {editingLabels && <BlockingEditor v={v} onDone={() => setEditingLabels(false)} />}
          <ul className="merge-rules">
            {checks.length > 0 && (
              <Yes on>
                {t("Required checks pass:")} <span className="mono">{checks.join(", ")}</span>
              </Yes>
            )}
            {v.ci_start.optional_checks.length > 0 && (
              <li className="small muted">
                {t("May stay unpassed:")} <span className="mono">{v.ci_start.optional_checks.join(", ")}</span>
              </li>
            )}
            {after.map((c) => (
              <Yes key={c} on>
                {conditionText(c, r, t)}
              </Yes>
            ))}
            {r.blocking_labels.length === 0 && <li className="small muted">{t("No label holds the merge back")}</li>}
            <Yes on={r.up_to_date || r.update_branch}>{t("Up to date with the base: the service merges it in before CI runs")}</Yes>
            <li className="small muted">
              {t("Merges with")} {r.method}
              {r.methods && ` (${t("allowed")}: ${r.methods.join(", ") || t("none")})`}
              {r.fork_approval && `; ${t("GitHub holds the workflows of")} ${t(FORK_APPROVAL[r.fork_approval] ?? r.fork_approval)}`}
            </li>
          </ul>
          {after.length > 0 && <span className="small muted">{t("A PR whose CI passed waits for these without holding the turn; then it merges on its next turn, merging the base in and running its CI again first if the base moved.")}</span>}
        </div>
      </div>
      {r.notes && <Markdown className="md merge-notes" text={r.notes} />}
    </Section>
  );
}

function BlockingEditor({ v, onDone }: { v: MergeView; onDone: () => void }) {
  const t = useT();
  const toast = useToast();
  const roles = Object.entries(v.rules.label_roles);
  const [picked, setPicked] = useState<string[]>(v.rules.blocking_roles);
  const save = useAct((r: string[]) => api.put<Saved>(`/api/merge-schedule/${v.repo}/blocking`, { roles: r }), [["merge-schedule"]]);
  return (
    <div className="stack merge-draft" style={{ gap: 6 }}>
      <span className="small muted">{t("Hold the merge back while a PR has one of these labels:")}</span>
      {roles.length === 0 && <span className="small muted">{t("The profile gives no labels to roles.")}</span>}
      {roles.map(([role, label]) => (
        <label key={role} className="row small" style={{ gap: 8 }}>
          <input type="checkbox" checked={picked.includes(role)} onChange={(e) => setPicked(e.target.checked ? [...picked, role] : picked.filter((x) => x !== role))} />
          <span className="mono">{label}</span>
          <span className="muted">({role})</span>
        </label>
      ))}
      <div className="row" style={{ gap: 8 }}>
        <button className="sm ghost right" onClick={onDone}>
          {t("Cancel")}
        </button>
        <button
          className="sm primary"
          disabled={save.isPending}
          onClick={() =>
            save.mutate(picked, {
              onSuccess: (out) => {
                toast({ title: out.applied ? t("Saved") : t("Proposed to the maintainers"), icon: out.applied ? "check" : "inbox" });
                onDone();
              },
            })
          }
        >
          {t("Save")}
        </button>
      </div>
      <ErrorBox error={save.error} />
    </div>
  );
}

function CiStartCard({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const s = v.ci_start;
  const investigate = useAct(() => api.post<{ event: string }>(`/api/merge-schedule/${v.repo}/investigate`), [["merge-schedule"]]);
  return (
    <Section
      title={t("How CI starts")}
      hint={t("What the scheduler acts on")}
      aside={
        v.can_arrange &&
        !editing && (
          <div className="row" style={{ gap: 6 }}>
            <button
              className="sm"
              disabled={investigate.isPending}
              title={t("An agent reads the workflows and the contribution guide and drafts this; you save the draft.")}
              onClick={() =>
                investigate.mutate(undefined, {
                  onSuccess: (out) => toast({ title: t("Investigating the CI"), body: t("The draft shows here when the agent is done."), icon: "sparkles", action: { label: t("Watch"), onClick: () => openJob(out.event) } }),
                })
              }
            >
              <Icon name="sparkles" size={13} />
              {t("Investigate")}
            </button>
            <button className="sm ghost" onClick={() => setEditing(true)}>
              {t("Edit")}
            </button>
          </div>
        )
      }
    >
      {editing ? (
        <CiStartForm v={v} onDone={() => setEditing(false)} />
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {s.how ? <Markdown className="md merge-notes" text={s.how} /> : <span className="small muted">{t("Not described yet. Investigate to have an agent read the workflows, or describe it yourself.")}</span>}
          <ul className="merge-rules">
            <li className="small">
              {t("Gate label")}: {s.gate_label ? <span className="mono">{s.gate_label}</span> : <span className="muted">{t("none: CI starts on push")}</span>}
            </li>
            {s.gate_jobs.length > 0 && (
              <li className="small">
                {t("Gate jobs (their failure means CI did not start)")}: <span className="mono">{s.gate_jobs.join(", ")}</span>
              </li>
            )}
            {s.summary_jobs.length > 0 && (
              <li className="small">
                {t("Summary jobs (never the failure)")}: <span className="mono">{s.summary_jobs.join(", ")}</span>
              </li>
            )}
            {s.optional_checks.length > 0 && (
              <li className="small">
                {t("Optional checks (may stay unpassed for a merge)")}: <span className="mono">{s.optional_checks.join(", ")}</span>
              </li>
            )}
            <li className="small muted">
              {t("Looked into when CI has not started after")} {s.start_minutes} {t("minutes, or still runs after")} {s.run_minutes}; {t("flakes are rerun up to")} {s.reruns} {t("times")}
            </li>
          </ul>
        </div>
      )}
      <ErrorBox error={investigate.error} />
    </Section>
  );
}

function CiStartForm({ v, onDone }: { v: MergeView; onDone: () => void }) {
  const t = useT();
  const toast = useToast();
  const d = null as Draft["draft"] | null;
  const s = v.ci_start;
  const [how, setHow] = useState(d?.how ?? s.how);
  const [label, setLabel] = useState(d ? d.gate_label : (s.gate_label ?? ""));
  const [gates, setGates] = useState((d?.gate_jobs ?? s.gate_jobs).join(", "));
  const [summaries, setSummaries] = useState((d?.summary_jobs ?? s.summary_jobs).join(", "));
  const [start, setStart] = useState(String(d?.start_minutes ?? s.start_minutes));
  const [run, setRun] = useState(String(d?.run_minutes ?? s.run_minutes));
  const [notes, setNotes] = useState(d?.merge_notes ?? v.rules.notes);
  const [optional, setOptional] = useState((d?.optional_checks ?? s.optional_checks).join(", "));
  const list = (x: string) => x.split(/[,\n]/).map((y) => y.trim()).filter(Boolean);
  const save = useAct(
    () =>
      api.put<Saved>(`/api/merge-schedule/${v.repo}/ci-start`, {
        how,
        gate_label: label.trim(),
        gate_jobs: list(gates),
        summary_jobs: list(summaries),
        start_minutes: Math.max(1, Number(start) || 5),
        run_minutes: Math.max(5, Number(run) || 180),
        merge_notes: notes,
        approvals: d?.approvals || null,
        optional_checks: list(optional),
        ready: d?.ready?.length ? d.ready : null,
      }),
    [["merge-schedule"]],
  );
  return (
    <div className="stack" style={{ gap: 8 }}>
      <label className="stack small" style={{ gap: 2 }}>
        {t("How CI starts, in plain words")}
        <textarea rows={4} value={how} onChange={(e) => setHow(e.target.value)} />
      </label>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <label className="stack small" style={{ gap: 2, flex: "1 1 140px" }}>
          {t("Gate label")}
          <input value={label} placeholder="run-ci" onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "0 1 120px" }}>
          {t("Start within (minutes)")}
          <input value={start} inputMode="numeric" onChange={(e) => setStart(e.target.value.replace(/\D/g, ""))} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "0 1 120px" }}>
          {t("Finish within (minutes)")}
          <input value={run} inputMode="numeric" onChange={(e) => setRun(e.target.value.replace(/\D/g, ""))} />
        </label>
      </div>
      <label className="stack small" style={{ gap: 2 }}>
        {t("Gate jobs (their failure means CI did not start)")}
        <input value={gates} placeholder="check-changes" onChange={(e) => setGates(e.target.value)} />
      </label>
      <label className="stack small" style={{ gap: 2 }}>
        {t("Summary jobs (never the failure)")}
        <input value={summaries} placeholder="ci-test-finish" onChange={(e) => setSummaries(e.target.value)} />
      </label>
      <label className="stack small" style={{ gap: 2 }}>
        {t("Optional checks (may stay unpassed for a merge)")}
        <input value={optional} placeholder="DCO" onChange={(e) => setOptional(e.target.value)} />
      </label>
      <label className="stack small" style={{ gap: 2 }}>
        {t("What the repository asks before a merge, beyond GitHub's rules")}
        <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </label>
      <div className="row" style={{ gap: 8 }}>
        <button className="ghost right" onClick={onDone}>
          {t("Cancel")}
        </button>
        <button
          className="primary"
          disabled={save.isPending}
          onClick={() =>
            save.mutate(undefined, {
              onSuccess: (out) => {
                toast({ title: out.applied ? t("Saved") : t("Proposed to the maintainers"), icon: out.applied ? "check" : "inbox" });
                onDone();
              },
            })
          }
        >
          {save.isPending ? t("Saving…") : t("Save")}
        </button>
      </div>
      <ErrorBox error={save.error} />
    </div>
  );
}

function MergedList({ v }: { v: MergeView }) {
  const t = useT();
  return (
    <details className="card merge-details">
      <summary>
        <strong>{t("Merged lately")}</strong> <span className="muted small">({v.merged.length})</span>
      </summary>
      <div className="list">
        {v.merged.map((m) => (
          <div key={`${m.number}:${m.at}`} className="list-row merge-row">
            <Icon name="merge" size={14} />
            <Link to={prPath(v.repo, m.number)} className="merge-pr">
              <span className="mono muted">#{m.number}</span> <span className="merge-title">{m.title}</span>
            </Link>
            <span className="small muted right">
              <When at={m.at} />
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function DescribeBox({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const [text, setText] = useState("");
  const describe = useAct((words: string) => api.post<{ event: string }>(`/api/merge-schedule/${v.repo}/describe`, { text: words }), [["merge-schedule"]]);
  return (
    <div className="stack" style={{ gap: 6 }}>
      <strong className="small">{t("Describe the rules in your words")}</strong>
      <textarea
        rows={3}
        value={text}
        placeholder={t("For example: schedulable once it has the approvals the branch needs and every comment is resolved; the action-required label does not block a merge; DCO may fail; rerun a flaky job twice, and every other failure once before it counts.")}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="row" style={{ gap: 8 }}>
        <span className="small muted">{t("An agent turns your words into the settings below and shows them for you to save.")}</span>
        <button
          className="sm primary right"
          disabled={describe.isPending || text.trim().length < 3}
          onClick={() =>
            describe.mutate(text.trim(), {
              onSuccess: (out) => {
                setText("");
                toast({ title: t("Turning your words into rules"), body: t("The draft shows on this page when the agent is done."), icon: "sparkles", action: { label: t("Watch"), onClick: () => openJob(out.event) } });
              },
            })
          }
        >
          <Icon name="sparkles" size={13} />
          {t("Turn into rules")}
        </button>
      </div>
      <ErrorBox error={describe.error} />
    </div>
  );
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

function DraftRules({ v, draft }: { v: MergeView; draft: Draft }) {
  const t = useT();
  const toast = useToast();
  // A draft from an older version may lack a list: read it as empty, never crash.
  const raw = draft.draft;
  const d = {
    ...raw,
    ready: raw.ready ?? [],
    blocking_roles: raw.blocking_roles ?? [],
    optional_checks: raw.optional_checks ?? [],
    gate_jobs: raw.gate_jobs ?? [],
    summary_jobs: raw.summary_jobs ?? [],
    flake_patterns: raw.flake_patterns ?? [],
    preserve_trailers: raw.preserve_trailers ?? [],
    required_workflows: raw.required_workflows ?? [],
  };
  const r = v.rules;
  const s = v.ci_start;
  const f = v.flaky;
  const labelOf = (role: string) => r.label_roles[role] ?? role;
  const list = (xs: string[]) => xs.join(", ") || "—";
  const conds = (xs: string[]) => xs.map((c) => conditionText(c, r, t)).reduce<React.ReactNode[]>((acc, x, i) => (i ? [...acc, "; ", x] : [x]), []);
  const judge = (x: string) => (x === "agent" ? t("flake patterns, then an agent's look") : t("flake patterns only"));
  const rows: { label: string; was: React.ReactNode; now: React.ReactNode; changed: boolean }[] = [
    { label: t("Schedulable when"), was: conds(r.ready), now: conds(d.ready), changed: !same([...r.ready].sort(), [...d.ready].sort()) },
    { label: t("Labels that hold a merge back"), was: list(r.blocking_roles.map(labelOf)), now: list(d.blocking_roles.map(labelOf)), changed: !same(r.blocking_roles, d.blocking_roles) },
    { label: t("Optional checks (may stay unpassed for a merge)"), was: list(s.optional_checks), now: list(d.optional_checks), changed: !same(s.optional_checks, d.optional_checks) },
    { label: t("Gate label"), was: s.gate_label || "—", now: d.gate_label || "—", changed: (s.gate_label || "") !== d.gate_label },
    { label: t("Gate jobs"), was: list(s.gate_jobs), now: list(d.gate_jobs), changed: !same(s.gate_jobs, d.gate_jobs) },
    { label: t("Summary jobs"), was: list(s.summary_jobs), now: list(d.summary_jobs), changed: !same(s.summary_jobs, d.summary_jobs) },
    { label: t("Start within (minutes)"), was: s.start_minutes, now: d.start_minutes, changed: s.start_minutes !== d.start_minutes },
    { label: t("Finish within (minutes)"), was: s.run_minutes, now: d.run_minutes, changed: s.run_minutes !== d.run_minutes },
    { label: t("A flake is judged by"), was: judge(f.diagnose), now: judge(d.diagnose), changed: f.diagnose !== d.diagnose },
    { label: t("Reruns of a flake per commit"), was: f.max_reruns, now: d.max_reruns, changed: f.max_reruns !== d.max_reruns },
    { label: t("Seconds between reruns"), was: f.backoff_seconds, now: d.backoff_seconds, changed: f.backoff_seconds !== d.backoff_seconds },
    { label: t("Reruns of any other failure first"), was: f.rerun_unknown, now: d.rerun_unknown, changed: f.rerun_unknown !== d.rerun_unknown },
    { label: t("Look for the root cause"), was: f.root_cause ? t("yes") : t("no"), now: d.root_cause ? t("yes") : t("no"), changed: f.root_cause !== d.root_cause },
    {
      label: t("Flake patterns"),
      was: list(f.patterns.map((p) => p.id)),
      now: list(d.flake_patterns.map((p) => p.id)),
      changed: !same(
        f.patterns.map((p) => [p.id, p.pattern, p.category]),
        d.flake_patterns.map((p) => [p.id, p.pattern, p.category]),
      ),
    },
    { label: t("Merges with"), was: r.method, now: d.merge_method, changed: r.method !== d.merge_method },
    { label: t("Commit subject"), was: r.subject, now: d.subject, changed: r.subject !== d.subject },
    { label: t("Trailers a squash keeps"), was: list(r.preserve_trailers), now: list(d.preserve_trailers), changed: !same(r.preserve_trailers, d.preserve_trailers) },
    { label: t("Merges the base in before merging"), was: r.update_branch ? t("yes") : t("no"), now: d.update_branch ? t("yes") : t("no"), changed: r.update_branch !== d.update_branch },
    { label: t("How many run their CI at once"), was: v.parallel, now: d.parallel, changed: v.parallel !== d.parallel },
    { label: t("Workflows that must pass"), was: list(r.required_workflows), now: list(d.required_workflows), changed: !same(r.required_workflows, d.required_workflows) },
    { label: t("A cancelled job counts as a failure"), was: r.cancelled_is_failure ? t("yes") : t("no"), now: d.cancelled_is_failure ? t("yes") : t("no"), changed: r.cancelled_is_failure !== d.cancelled_is_failure },
    { label: t("How CI starts"), was: s.how ? t("described") : "—", now: d.how ? t("described anew, below") : "—", changed: (s.how || "") !== d.how },
    { label: t("Before merging"), was: r.notes || "—", now: d.merge_notes || "—", changed: (r.notes || "") !== d.merge_notes },
  ];
  const changed = rows.filter((x) => x.changed);
  const save = useAct(() => api.post<Saved>(`/api/merge-schedule/${v.repo}/draft/apply`), [["merge-schedule"]]);
  const discard = useAct(() => api.del(`/api/merge-schedule/${v.repo}/draft`), [["merge-schedule"]]);
  return (
    <Section
      title={t("Draft rules")}
      hint={`${draft.source === "words" ? t("from your words") : t("from an investigation")} · ${draft.by}`}
      aside={
        <span className="small muted">
          <When at={draft.at} />
        </span>
      }
    >
      <div className="stack" style={{ gap: 10 }}>
        {draft.words && <blockquote className="small merge-words">{draft.words}</blockquote>}
        {changed.length === 0 ? (
          <span className="small muted">{t("It changes nothing: the settings already say this.")}</span>
        ) : (
          <table className="merge-diff small">
            <thead>
              <tr>
                <th>{t("Setting")}</th>
                <th>{t("Now")}</th>
                <th>{t("Draft")}</th>
              </tr>
            </thead>
            <tbody>
              {changed.map((x) => (
                <tr key={x.label}>
                  <td>{x.label}</td>
                  <td className="muted">{x.was}</td>
                  <td>
                    <strong>{x.now}</strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {(s.how || "") !== d.how && d.how && <Markdown className="md merge-notes" text={d.how} />}
        {(draft.dropped ?? []).length > 0 && (
          <span className="small muted">
            {t("Left out, as the repository does not have them")}: {draft.dropped.join("; ")}
          </span>
        )}
        {v.can_arrange && (
          <div className="row" style={{ gap: 8 }}>
            <button className="sm ghost" disabled={discard.isPending} onClick={() => discard.mutate(undefined)}>
              {t("Discard")}
            </button>
            <button
              className="sm primary right"
              disabled={save.isPending || changed.length === 0}
              onClick={() =>
                save.mutate(undefined, {
                  onSuccess: (out) => toast({ title: out.applied ? t("Saved") : t("Proposed to the maintainers"), icon: out.applied ? "check" : "inbox" }),
                })
              }
            >
              {t("Save these rules")}
            </button>
          </div>
        )}
        <ErrorBox error={save.error || discard.error} />
      </div>
    </Section>
  );
}

function FlakyCard({ v }: { v: MergeView }) {
  const t = useT();
  const toast = useToast();
  const f = v.flaky;
  const [editing, setEditing] = useState(false);
  const [diagnose, setDiagnose] = useState(f.diagnose);
  const [maxReruns, setMaxReruns] = useState(String(f.max_reruns));
  const [backoff, setBackoff] = useState(String(f.backoff_seconds));
  const [unknown, setUnknown] = useState(String(f.rerun_unknown));
  const [rootCause, setRootCause] = useState(f.root_cause);
  const [adding, setAdding] = useState(false);
  const [pid, setPid] = useState("");
  const [pattern, setPattern] = useState("");
  const [category, setCategory] = useState<"flaky" | "infra">("flaky");
  const [description, setDescription] = useState("");
  const done = (out: Saved) => toast({ title: out.applied ? t("Saved") : t("Proposed to the maintainers"), icon: out.applied ? "check" : "inbox" });
  const save = useAct(
    () =>
      api.put<Saved>(`/api/merge-schedule/${v.repo}/flaky`, {
        diagnose,
        max_reruns: Math.min(10, Number(maxReruns) || 0),
        backoff_seconds: Math.max(0, Number(backoff) || 0),
        rerun_unknown: Math.min(5, Number(unknown) || 0),
        root_cause: rootCause,
      }),
    [["merge-schedule"]],
  );
  const own = f.patterns.filter((p) => p.own);
  const dismiss = useAct((sid: string) => api.del(`/api/merge-schedule/${v.repo}/suggestions/${encodeURIComponent(sid)}`), [["merge-schedule"]]);
  const patterns = useAct((next: Pattern[]) => api.put<Saved>(`/api/merge-schedule/${v.repo}/flake-patterns`, { patterns: next.map(({ id, pattern, category, description }) => ({ id, pattern, category, description })) }), [["merge-schedule"]]);
  return (
    <Section
      title={t("Flaky tests and reruns")}
      hint={t("How a failed job of a scheduled PR, or one someone asks about, is judged and run again")}
      aside={
        v.can_arrange &&
        !editing && (
          <button className="sm ghost" onClick={() => setEditing(true)}>
            {t("Edit")}
          </button>
        )
      }
    >
      {editing ? (
        <div className="stack" style={{ gap: 8 }}>
          <label className="stack small" style={{ gap: 2 }}>
            {t("A flake is judged by")}
            <select value={diagnose} onChange={(e) => setDiagnose(e.target.value as "agent" | "patterns")}>
              <option value="agent">{t("flake patterns, then an agent's look")}</option>
              <option value="patterns">{t("flake patterns only")}</option>
            </select>
          </label>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <label className="stack small" style={{ gap: 2, flex: "1 1 140px" }}>
              {t("Reruns of a flake per commit")}
              <input value={maxReruns} inputMode="numeric" onChange={(e) => setMaxReruns(e.target.value.replace(/\D/g, ""))} />
            </label>
            <label className="stack small" style={{ gap: 2, flex: "1 1 140px" }}>
              {t("Seconds between reruns")}
              <input value={backoff} inputMode="numeric" onChange={(e) => setBackoff(e.target.value.replace(/\D/g, ""))} />
            </label>
            <label className="stack small" style={{ gap: 2, flex: "1 1 140px" }}>
              {t("Reruns of any other failure first")}
              <input value={unknown} inputMode="numeric" onChange={(e) => setUnknown(e.target.value.replace(/\D/g, ""))} />
            </label>
          </div>
          <label className="row small" style={{ gap: 8 }}>
            <input type="checkbox" checked={rootCause} onChange={(e) => setRootCause(e.target.checked)} />
            {t("Look for the root cause of a real failure (an agent reads the PR's code)")}
          </label>
          <div className="row" style={{ gap: 8 }}>
            <button className="sm ghost right" onClick={() => setEditing(false)}>
              {t("Cancel")}
            </button>
            <button className="sm primary" disabled={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: (out) => (done(out), setEditing(false)) })}>
              {t("Save")}
            </button>
          </div>
          <ErrorBox error={save.error} />
        </div>
      ) : (
        <ul className="merge-rules">
          <li className="small">
            {t("A flake is judged by")}: <strong>{f.diagnose === "agent" ? t("flake patterns, then an agent's look") : t("flake patterns only")}</strong>
          </li>
          <li className="small">
            {t("Reruns of a flake per commit")}: <strong>{f.max_reruns}</strong>, {f.backoff_seconds} {t("seconds apart")}
          </li>
          <li className="small">
            {t("Reruns of any other failure first")}: <strong>{f.rerun_unknown}</strong>
          </li>
          <li className="small">
            {t("Look for the root cause")}: <strong>{f.root_cause ? t("yes") : t("no")}</strong>
          </li>
        </ul>
      )}
      <div className="stack" style={{ gap: 6, marginTop: 12 }}>
        <div className="row" style={{ gap: 8 }}>
          <strong className="small">{t("Flake patterns")}</strong>
          <span className="small muted">({f.patterns.length})</span>
          {v.can_arrange && !adding && (
            <button className="sm ghost right" onClick={() => setAdding(true)}>
              <Icon name="plus" size={13} />
              {t("Add")}
            </button>
          )}
        </div>
        {adding && (
          <div className="stack merge-draft" style={{ gap: 6 }}>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <input style={{ flex: "1 1 120px" }} value={pid} placeholder={t("id, such as hub-timeout")} onChange={(e) => setPid(e.target.value)} />
              <select value={category} onChange={(e) => setCategory(e.target.value as "flaky" | "infra")}>
                <option value="flaky">{t("flaky")}</option>
                <option value="infra">{t("the runner's")}</option>
              </select>
            </div>
            <input className="mono" value={pattern} placeholder="ReadTimeoutError: HTTPSConnectionPool\(host='huggingface.co'" onChange={(e) => setPattern(e.target.value)} />
            <input value={description} placeholder={t("What it is")} onChange={(e) => setDescription(e.target.value)} />
            <div className="row" style={{ gap: 8 }}>
              <button className="sm ghost right" onClick={() => setAdding(false)}>
                {t("Cancel")}
              </button>
              <button
                className="sm primary"
                disabled={patterns.isPending || !pid.trim() || !pattern.trim()}
                onClick={() =>
                  patterns.mutate([...own, { id: pid.trim(), pattern, category, description }], {
                    onSuccess: (out) => {
                      done(out);
                      setAdding(false);
                      setPid("");
                      setPattern("");
                      setDescription("");
                    },
                  })
                }
              >
                {t("Save")}
              </button>
            </div>
          </div>
        )}
        {f.suggestions.length > 0 && (
          <div className="stack merge-draft" style={{ gap: 6 }}>
            <span className="small">
              <Icon name="sparkles" size={12} /> {t("Learned from failures marked flaky")}
            </span>
            {f.suggestions.map((g) => (
              <div key={g.id} className="list-row merge-row">
                <div className="stack merge-main">
                  <span className="small">
                    <strong>{g.id}</strong> <span className="muted">· {g.from} · {t("marked by")} {g.by}</span>
                  </span>
                  <code className="small mono" style={{ overflowWrap: "anywhere" }}>
                    {g.pattern}
                  </code>
                  {g.description && <span className="small muted">{g.description}</span>}
                </div>
                {v.can_arrange && (
                  <div className="row merge-actions">
                    <button
                      className="sm primary"
                      disabled={patterns.isPending}
                      onClick={() =>
                        patterns.mutate([...own, { id: g.id, pattern: g.pattern, category: g.category, description: g.description }], {
                          onSuccess: (out) => {
                            done(out);
                            dismiss.mutate(g.id);
                          },
                        })
                      }
                    >
                      {t("Add")}
                    </button>
                    <button className="sm ghost" disabled={dismiss.isPending} onClick={() => dismiss.mutate(g.id)}>
                      {t("Dismiss")}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {f.patterns.length === 0 ? (
          <span className="small muted">{t("No flake patterns yet.")}</span>
        ) : (
          <div className="list">
            {f.patterns.map((p) => (
              <div key={p.id} className="list-row merge-row">
                <div className="stack merge-main">
                  <span className="small">
                    <strong>{p.id}</strong> <span className="muted">· {p.category === "infra" ? t("the runner's") : t("flaky")}</span>
                    {!p.own && <span className="muted"> · {t("from a pack")}</span>}
                  </span>
                  <code className="small mono" style={{ overflowWrap: "anywhere" }}>
                    {p.pattern}
                  </code>
                  {p.description && <span className="small muted">{p.description}</span>}
                </div>
                {v.can_arrange && p.own && (
                  <button className="sm ghost" disabled={patterns.isPending} onClick={() => patterns.mutate(own.filter((x) => x.id !== p.id), { onSuccess: done })}>
                    {t("Remove")}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        <ErrorBox error={patterns.error} />
      </div>
    </Section>
  );
}
