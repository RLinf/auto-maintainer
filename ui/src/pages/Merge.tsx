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

type Phase = "queued" | "updating" | "starting" | "running" | "rerunning" | "merging" | "failed" | "held" | "merged";

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
  };
  dropped: string[];
  by: string;
  at: string;
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
  };
  ci_start: CiStart;
  draft: Draft | null;
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
  const q = useQuery({
    queryKey: ["merge-schedule", scope],
    queryFn: () => api.get<MergeView>(`/api/merge-schedule/${scope}/view`),
    enabled: !!scope,
    refetchInterval: 20_000,
  });
  const v = q.data;
  return (
    <div className="page">
      <PageHead
        title={t("Merge scheduling")}
        sub={t("Choose which ready PRs merge and in what order. The service starts their CI the way the repository starts it, reruns what flakes, sets real failures aside with their cause, and merges what passes.")}
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
        <div className="stack" style={{ gap: 16 }}>
          <Controls v={v} />
          <ScheduleSection v={v} />
          {v.failed.length > 0 && <FailureLane v={v} />}
          <Candidates v={v} />
          <NotReadyList v={v} />
          <div className="merge-grid">
            <RulesCard v={v} />
            <CiStartCard v={v} />
          </div>
          {v.merged.length > 0 && <MergedList v={v} />}
        </div>
      )}
    </div>
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
                    </span>
                  </span>
                  {s.approval_needed && (
                    <span className="small" style={{ color: "var(--warn)" }}>
                      {t("GitHub holds this fork's workflows until a person approves running them:")} <Link to={prPath(v.repo, s.number)}>{t("Run CI on the PR page")}</Link>
                    </span>
                  )}
                  {s.finding && <span className="small merge-finding">{s.finding}</span>}
                </div>
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
            {v.can_arrange && (
              <div className="row merge-actions">
                <FixCiButton repo={v.repo} number={f.number} failed={f.failed_jobs.length ? f.failed_jobs : f.ci.failed} canPush={v.signed_in} size="sm" />
                <button className="sm" disabled={retry.isPending} onClick={() => retry.mutate(f.number)}>
                  {t("Retry")}
                </button>
                <button className="sm ghost" disabled={remove.isPending} onClick={() => remove.mutate(f.number)}>
                  {t("Unschedule")}
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
      <ErrorBox error={retry.error || remove.error} />
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
      title={t("Ready to merge")}
      hint={v.auto ? t("Automatic scheduling adds these as they become ready.") : t("Ready by the repository's rules; their CI runs once scheduled.")}
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
        <Empty icon="check">{t("No PR is ready to merge right now.")}</Empty>
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

function RulesCard({ v }: { v: MergeView }) {
  const t = useT();
  const r = v.rules;
  const checks = [...(r.required_checks ?? []), ...r.required_workflows.map((w) => `${w} (${t("workflow")})`)];
  return (
    <Section title={t("Ready to merge when")} hint={t("Read from GitHub's branch rules and the repository's profile")}>
      <ul className="merge-rules">
        <li className="small">
          <Icon name="check" size={12} /> {r.approvals} {t("approvals from people with write access, and no changes requested")}
        </li>
        <Yes on={r.code_owners}>{t("A code owner of each changed file approves")}</Yes>
        <Yes on={r.last_push}>{t("Someone other than the last pusher approves the last push")}</Yes>
        <Yes on={r.threads}>{t("Every review conversation is resolved")}</Yes>
        <li className="small">
          <Icon name="check" size={12} /> {t("Not a draft, and no conflict with the base")}
          {r.blocking_labels.length > 0 && `; ${t("none of the labels")} ${r.blocking_labels.join(", ")}`}
        </li>
        {checks.length > 0 && (
          <li className="small">
            <Icon name="check" size={12} /> {t("Required checks pass:")} <span className="mono">{checks.join(", ")}</span>
          </li>
        )}
        <Yes on={r.up_to_date || r.update_branch}>{t("Up to date with the base: the service merges it in before CI runs")}</Yes>
        <li className="small muted">
          {t("Merges with")} {r.method}
          {r.methods && ` (${t("allowed")}: ${r.methods.join(", ") || t("none")})`}
          {r.fork_approval && `; ${t("GitHub holds the workflows of")} ${t(FORK_APPROVAL[r.fork_approval] ?? r.fork_approval)}`}
        </li>
      </ul>
      {r.notes && <Markdown className="md merge-notes" text={r.notes} />}
    </Section>
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
            <li className="small muted">
              {t("Looked into when CI has not started after")} {s.start_minutes} {t("minutes, or still runs after")} {s.run_minutes}; {t("flakes are rerun up to")} {s.reruns} {t("times")}
            </li>
          </ul>
          {v.draft && <DraftBox v={v} draft={v.draft} onUse={() => setEditing(true)} />}
        </div>
      )}
      <ErrorBox error={investigate.error} />
    </Section>
  );
}

function DraftBox({ v, draft, onUse }: { v: MergeView; draft: Draft; onUse: () => void }) {
  const t = useT();
  return (
    <div className="merge-draft stack" style={{ gap: 6 }}>
      <span className="small">
        <Icon name="sparkles" size={12} /> {t("An agent's draft")} · {draft.by} · <When at={draft.at} />
      </span>
      <Markdown className="md merge-notes" text={draft.draft.how} />
      <span className="small muted">
        {t("Gate label")}: {draft.draft.gate_label || "—"} · {t("Gate jobs")}: {draft.draft.gate_jobs.join(", ") || "—"} · {t("Summary jobs")}: {draft.draft.summary_jobs.join(", ") || "—"}
      </span>
      {draft.draft.merge_notes && (
        <span className="small">
          {t("Before merging")}: {draft.draft.merge_notes}
        </span>
      )}
      {draft.dropped.length > 0 && (
        <span className="small muted">
          {t("Left out, as the repository does not have them")}: {draft.dropped.join("; ")}
        </span>
      )}
      {v.can_arrange && (
        <div>
          <button className="sm primary" onClick={onUse}>
            {t("Review and save")}
          </button>
        </div>
      )}
    </div>
  );
}

function CiStartForm({ v, onDone }: { v: MergeView; onDone: () => void }) {
  const t = useT();
  const toast = useToast();
  const d = v.draft?.draft;
  const s = v.ci_start;
  const [how, setHow] = useState(d?.how ?? s.how);
  const [label, setLabel] = useState(d ? d.gate_label : (s.gate_label ?? ""));
  const [gates, setGates] = useState((d?.gate_jobs ?? s.gate_jobs).join(", "));
  const [summaries, setSummaries] = useState((d?.summary_jobs ?? s.summary_jobs).join(", "));
  const [start, setStart] = useState(String(d?.start_minutes ?? s.start_minutes));
  const [run, setRun] = useState(String(d?.run_minutes ?? s.run_minutes));
  const [notes, setNotes] = useState(d?.merge_notes ?? v.rules.notes);
  const list = (x: string) => x.split(/[,\n]/).map((y) => y.trim()).filter(Boolean);
  const save = useAct(
    () =>
      api.put<Saved>(`/api/merge-schedule/${v.repo}/ci-start`, {
        how,
        gate_label: label.trim(),
        gate_jobs: list(gates),
        summary_jobs: list(summaries),
        start_minutes: Math.max(1, Number(start) || 20),
        run_minutes: Math.max(5, Number(run) || 360),
        merge_notes: notes,
        approvals: d?.approvals || null,
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
