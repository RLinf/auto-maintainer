# Copyright 2026 The RLinf Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CI doctor (design 5.5): classify failed jobs, rerun flakes, and find the root cause
of real failures. It runs only when someone asks (``CiDoctor``) and for a scheduled
PR (``playbooks/merge.py``, through ``first_looks`` and ``find_causes``): CI of PRs
nobody asked about costs no agent tokens.

The failed jobs are those of the newest run of each workflow at the head, without
the repository's gate and summary jobs (``failing_checks``). Each failed job gets a first look from its log: a flake signature, the first look at
the same failure seen before (``fingerprint``: the same job failing with the same
error lines, at this head of this PR, or as a flake or a runner failure anywhere in
the repository in the last ``REUSE_DAYS``), or an agent's classification, one call
for all of a run's new failures. Flakes are rerun within the profile's budget. The
rest, and flakes that keep failing after their reruns, go together to a root-cause
agent working in a read-only checkout of the PR head, which names what fails, the
mechanism, where in the code, and the smallest fix; a runner failure does not (the
code cannot explain it), and a failure whose cause was found at this head before
keeps it. A cause in the change is explained to the PR author; one that is not (the
base branch, the runner, unclear) goes to the maintainers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionHints,
    ActionKind,
    ActionStatus,
    CommentUpsertPayload,
    NotifyPayload,
    RerunPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
from ..domain.memory import Signal, SignalKind
from ..domain.profile import FlakeSignature, RepoProfile
from ..domain.records import CiObservation, RootCause
from ..domain.run_ci import ci_view
from ..domain.snapshot import CheckRun, PrSnapshot
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import (
    pr_block,
    schema_hint,
    untrusted,
    without_generated,
    work_lessons_block,
)

_ERROR_RE = re.compile(
    r"(error|failed|failure|exception|traceback|assert|fatal)", re.IGNORECASE
)
_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*")
_VOLATILE_RE = re.compile(
    r"0x[0-9a-f]+|\b[0-9a-f]{7,40}\b|/tmp/\S+|\d+(\.\d+)?", re.IGNORECASE
)
REUSE_DAYS = 7
"""How long a flake's or a runner failure's first look stands for the same failure."""
VERDICT_DAYS = 90
"""How long a person's verdict on a failure stands for the same failure, and serves
the first look as an example."""
LESSONS_KEPT = 12


def lessons(judged: list[CiObservation]) -> str:
    """The repository's latest verdicts from people, as examples for the first look:
    which failures its maintainers called flaky and which real."""
    rows = [
        f"- {o.check_name}: {o.verdict}"
        + (f" ({o.explanation[:200]})" if o.explanation else "")
        for o in judged[-LESSONS_KEPT:]
    ]
    if not rows:
        return ""
    # The verdicts are people's; the explanations beside them came from logs.
    return (
        "How this repository's maintainers judged earlier failures (their word, to "
        "learn from; a failure like one of these is likely the same):\n"
        + untrusted("ci.verdicts", "\n".join(rows), 4_000)
    )


FROM_THE_LOG = (
    "Judge only from the log and the PR given here: do not run commands, read "
    "files, or fetch logs, runs, or code."
)
"""What keeps a first look at a failure to one answer: agents offered tools open the
full log, other runs, and the code, at many times the cost (design 5.5)."""


class SuggestedSignature(BaseModel):
    pattern: str
    workflow: str | None = None
    job: str | None = None
    description: str = ""


class Diagnosis(BaseModel):
    category: Literal["flaky", "infra", "pr_caused", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list, description="Up to five log lines."
    )
    explanation: str = Field(
        description="For pr_caused: a short note for the PR author naming the failing test and likely cause."
    )
    signature: SuggestedSignature | None = None


def excerpt(log: str, max_lines: int = 200) -> str:
    """The lines around the first error, plus the tail of the log."""
    lines = log.splitlines()
    first = next(
        (i for i, line in enumerate(lines) if _ERROR_RE.search(line)),
        max(0, len(lines) - max_lines),
    )
    start = max(0, first - 40)
    head = lines[start : start + max_lines // 2]
    tail = lines[-max_lines // 2 :]
    return "\n".join(
        head + ["…"] + tail
        if start + max_lines // 2 < len(lines) - max_lines // 2
        else lines[start:]
    )


def fingerprint(log: str, check: CheckRun) -> str | None:
    """The failure's identity across runs and PRs: the job, and its first error lines
    with what changes from run to run (times, numbers, hashes, temporary paths)
    taken out; ``None`` when the log names no error."""
    lines = [_STAMP_RE.sub("", line).strip() for line in log.splitlines()]
    errors = [line for line in lines if _ERROR_RE.search(line)][:8]
    if not errors:
        return None
    text = "\n".join(
        [check.workflow or "", check.name, *(_VOLATILE_RE.sub("#", e) for e in errors)]
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def match_signature(
    log: str, check: CheckRun, signatures: list[FlakeSignature]
) -> FlakeSignature | None:
    for sig in signatures:
        if sig.workflow and sig.workflow != check.workflow:
            continue
        if sig.job and sig.job != check.name:
            continue
        try:
            if re.search(sig.pattern, log, re.IGNORECASE | re.MULTILINE):
                return sig
        except re.error:
            continue
    return None


class JobDiagnosis(Diagnosis):
    job: str = Field(description="The job's name, as given.")


class Diagnoses(BaseModel):
    """The first look at several failed jobs of one run, one entry per job."""

    jobs: list[JobDiagnosis] = Field(default_factory=list)


class RootCauseReport(BaseModel):
    """What the root-cause agent returns: one entry per distinct cause."""

    causes: list[RootCause] = Field(default_factory=list)


@dataclass
class Failure:
    """A failed job of this run and what the first look at its log found."""

    check: CheckRun
    sig: FlakeSignature | None
    diagnosis: Diagnosis
    log: str
    reruns: int = 0
    """How many reruns it already failed after, when it was taken for a flake."""
    fingerprint: str | None = None
    cause: RootCause | None = None
    """The root cause found for the same failure at this head before."""

    @property
    def real(self) -> bool:
        """Not a flake, or a flake that kept failing: worth finding the cause of."""
        return self.diagnosis.category != "flaky" or self.reruns > 0

    @property
    def needs_cause(self) -> bool:
        """Real, not the runner's (the code cannot explain that), and with no cause
        found before."""
        return self.real and self.diagnosis.category != "infra" and self.cause is None


MAX_CULPRITS = 5


def render_cause(cause: RootCause, base: str) -> str:
    """A root cause as the PR author reads it in the CI comment."""
    jobs = ", ".join(cause.jobs)
    if cause.origin == "base":
        lines = [
            f"- **{cause.failing}** ({jobs}): fails on `{base}` too, so it does not "
            f"come from this PR. {cause.cause}"
        ]
    else:
        lines = [f"- **{cause.failing}** ({jobs}): {cause.cause}"]
    for c in cause.culprits:
        where = f"{c.path}:{c.line}" if c.line else c.path
        lines.append(f"  - `{where}`" + (f": {c.why}" if c.why else ""))
    if cause.fix and cause.origin == "change":
        lines.append(f"  - Suggested fix: {cause.fix}")
    return "\n".join(lines)


def accepted_causes(
    report: RootCauseReport, jobs: list[str], changed: list[str], workdir: Path
) -> list[RootCause]:
    """The agent's causes, checked: only jobs that failed here, and only culprit paths
    that the PR changes or that exist in the checkout (never outside it)."""
    root = workdir.resolve()
    out: list[RootCause] = []
    for cause in report.causes:
        named = [j for j in cause.jobs if j in jobs]
        if not named and len(jobs) == 1:
            named = list(jobs)
        if not named:
            continue
        culprits = []
        for c in cause.culprits:
            target = (root / c.path).resolve()
            inside = target == root or root in target.parents
            if c.path in changed or (inside and target.is_file()):
                culprits.append(c)
        out.append(
            cause.model_copy(
                update={
                    "jobs": named,
                    "culprits": culprits[:MAX_CULPRITS],
                    "evidence": cause.evidence[:5],
                }
            )
        )
    return out


def _rerun_requested(event: Event) -> bool:
    return event.kind == EventKind.PR_COMMENT and any(
        c.get("name") == "rerun" for c in event.payload.get("commands", [])
    )


def _seen_before(
    seen: list[CiObservation], pr: PrSnapshot, check: CheckRun, mark: str | None
) -> CiObservation | None:
    """The latest first look at the same failure: at this head of this PR whatever
    it found, or anywhere in the repository when it was a flake or the runner's.
    Never a signature's (that is matched again) nor an agent's that found nothing."""
    if mark is None:
        return None
    for obs in reversed(seen):
        if (
            obs.fingerprint != mark
            or obs.check_name != check.name
            or obs.signature
            or obs.confidence == 0.0
        ):
            continue
        here = obs.number == pr.number and same_commit(obs.sha, pr.head_sha)
        if here or obs.category in ("flaky", "infra"):
            return obs
    return None


def _same_head_cause(before: CiObservation | None, pr: PrSnapshot) -> RootCause | None:
    """The root cause found before for the same failure at this head of this PR."""
    if (
        before is None
        or before.root_cause is None
        or before.number != pr.number
        or not same_commit(before.sha, pr.head_sha)
    ):
        return None
    return before.root_cause


async def first_looks(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, checks: list[CheckRun]
) -> list[Failure]:
    """The first look at each of ``checks`` (failed jobs at the head): a flake
    signature, the first look at the same failure seen before, or one agent call
    for the rest, from their logs alone."""
    signatures = await ctx.knowledge.flake_signatures(pr.repo, profile)
    seen = await ctx.store.list_ci_observations(
        pr.repo, since=ctx.clock.now() - timedelta(days=REUSE_DAYS)
    )
    judged = [
        o
        for o in await ctx.store.list_ci_observations(
            pr.repo, since=ctx.clock.now() - timedelta(days=VERDICT_DAYS)
        )
        if o.verdict
    ]
    failures: list[Failure] = []
    new: list[tuple[CheckRun, str, FlakeSignature | None, str | None]] = []
    for check in checks:
        log = await ctx.ci.log_of(pr.repo, check) or check.summary or ""
        sig = match_signature(log, check, signatures)
        mark = fingerprint(log, check)
        word = next(
            (
                o
                for o in reversed(judged)
                if mark and o.fingerprint == mark and o.check_name == check.name
            ),
            None,
        )
        before = _seen_before(seen, pr, check, mark) if sig is None else None
        if word is not None:
            # A person's word on the same failure outweighs patterns and agents.
            diagnosis = Diagnosis(
                category="flaky" if word.verdict == "flaky" else "pr_caused",
                confidence=1.0,
                explanation=f"{word.verdict_by} marked the same failure "
                + ("flaky" if word.verdict == "flaky" else "a real failure")
                + (f": {word.explanation}" if word.explanation else ""),
            )
            before = None
        elif sig is not None:
            diagnosis = Diagnosis(
                category=sig.category,
                confidence=0.9,
                explanation=sig.description or f"matched {sig.id}",
            )
        elif before is not None:
            diagnosis = Diagnosis(
                category=before.category,  # type: ignore[arg-type]
                confidence=before.confidence,
                explanation=before.explanation,
            )
        else:
            new.append((check, log, sig, mark))
            continue
        failures.append(
            Failure(
                check,
                sig,
                diagnosis,
                log,
                fingerprint=mark,
                cause=_same_head_cause(before, pr),
            )
        )
    # ``ci.diagnose: patterns`` keeps agents off the logs: what no pattern names is
    # not a flake.
    found = (
        await _diagnose_all(
            ctx, pr, [(c, excerpt(log)) for c, log, _, _ in new], lessons(judged)
        )
        if profile.ci.diagnose == "agent"
        else {}
    )
    for check, log, sig, mark in new:
        diagnosis = found.get(check.name) or Diagnosis(
            category="unknown",
            confidence=0.0,
            explanation="no diagnosis"
            if profile.ci.diagnose == "agent"
            else "no flake pattern matches its log",
        )
        failures.append(Failure(check, sig, diagnosis, log, fingerprint=mark))
    return failures


async def find_causes(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    profile: RepoProfile,
    failures: list[Failure],
) -> tuple[list[RootCause] | None, str]:
    """The root causes of ``failures`` that are real (``Failure.real``), and the base
    branch they were compared against; ``None`` when none was asked for or found."""
    real = [f for f in failures if f.real]
    causes: list[RootCause] | None = None
    base = "main"
    asked = [f for f in real if f.needs_cause] if profile.ci.root_cause else []
    known = [f.cause for f in real if f.cause is not None]
    if asked or known:
        base = (await ctx.forge.get_repo(pr.repo)).default_branch
    if asked:
        causes = await _root_cause(ctx, pr, asked, base)
    if known:
        unique = {c.model_dump_json(): c for c in known}
        causes = [*(causes or []), *unique.values()]
    return causes, base


async def record(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    failures: list[Failure],
    causes: list[RootCause] | None,
) -> None:
    """Keep each failure's first look and root cause; the PR page and the CI page
    show them."""
    by_job = {job: cause for cause in causes or [] for job in cause.jobs}
    for f in failures:
        await ctx.store.add_ci_observation(
            CiObservation(
                repo=pr.repo,
                number=pr.number,
                sha=pr.head_sha,
                run_id=f.check.run_id,
                check_id=f.check.id,
                check_name=f.check.name,
                workflow=f.check.workflow,
                details_url=f.check.details_url,
                category=f.diagnosis.category,
                signature=f.sig.id if f.sig else None,
                confidence=f.diagnosis.confidence,
                explanation=f.diagnosis.explanation,
                rerun=f.diagnosis.category == "flaky" and not f.reruns,
                root_cause=by_job.get(f.check.name),
                fingerprint=f.fingerprint,
            )
        )


def explained(
    failures: list[Failure], causes: list[RootCause] | None, base: str
) -> list[str]:
    """What the PR author reads about ``failures``: each root cause, else the first
    look of each real failure that the change caused or kept failing."""
    lines = [
        render_cause(c, base) for c in causes or [] if c.origin in ("change", "base")
    ]
    by_job = {job for cause in causes or [] for job in cause.jobs}
    for f in failures:
        if not f.real or f.check.name in by_job:
            continue
        if f.reruns:
            lines.append(
                f"- **{f.check.name}** failed again after {f.reruns} reruns: "
                f"{f.diagnosis.explanation}"
            )
        elif f.diagnosis.category == "pr_caused":
            lines.append(f"- **{f.check.name}**: {f.diagnosis.explanation}")
    return lines


def failing_checks(pr: PrSnapshot, profile: RepoProfile) -> list[CheckRun]:
    """The failed jobs at ``pr``'s head worth a look: in the newest run of each
    workflow, never the repository's gate or summary jobs (``ci.start``), and
    cancelled ones only under ``ci.cancelled_is_failure``."""
    view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
    checks = list(view.failed)
    if profile.ci.cancelled_is_failure:
        checks += [c for c in view.cancelled if c not in checks]
    return checks


async def outside_logs(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    profile: RepoProfile,
    chosen: set[str] | None = None,
) -> str:
    """The logs of ``pr``'s failed checks that an outside service reported as commit
    statuses (Read the Docs), those in ``chosen`` when a person chose, read by the
    service: an agent's tools reach only the forge's own CI. Empty when none failed
    or the forge has no log for them."""
    out = []
    for c in failing_checks(pr, profile):
        if c.workflow is not None or c.run_id is not None:
            continue
        if chosen and c.name not in chosen:
            continue
        log = await ctx.ci.log_of(pr.repo, c)
        if log:
            out.append(f"{c.name}:\n{log[-15_000:]}")
    return "\n\n".join(out)


OUTSIDE_LOGS = (
    "Logs of the failed checks outside services reported, read by the service "
    "(your tools cannot reach them):\n"
)


async def reruns_at_head(ctx: PlaybookContext, pr: PrSnapshot, run_id: int) -> int:
    """The reruns of ``run_id`` that ran at ``pr``'s head."""
    records = await ctx.store.list_actions(
        repo=pr.repo,
        number=pr.number,
        kinds=[ActionKind.CI_RERUN_FAILED],
        statuses=[ActionStatus.EXECUTED],
    )
    return sum(
        1
        for r in records
        if r.action.payload.get("run_id") == run_id
        and same_commit(r.action.head_sha, pr.head_sha)
    )


class CiDoctor(Playbook):
    """The CI doctor on request: a person's Explain failures on the PR page
    (``dashboard.command`` with ``command: ci_diagnose``), or ``/am rerun``. A
    scheduled PR's failures go through the same steps in ``playbooks/merge.py``;
    nothing else diagnoses CI on its own."""

    name = "ci_doctor"
    triggers = (EventKind.DASHBOARD_COMMAND, EventKind.PR_COMMENT)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if event.kind == EventKind.PR_COMMENT:
            return _rerun_requested(event)
        return (
            event.payload.get("command") == "ci_diagnose"
            and bool(event.payload.get("by"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        if event.kind == EventKind.PR_COMMENT:
            return f"ci_doctor:{event.repo}#{event.number}:rerun:{event.payload.get('comment_id')}"
        return f"ci_doctor:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        if event.kind == EventKind.PR_COMMENT:
            return await self._manual_rerun(ctx, pr, event)
        by = str(event.payload.get("by"))
        ctx.requested_by = by
        checks = failing_checks(pr, profile)
        chosen = set(event.payload.get("jobs") or [])
        if chosen:
            # The jobs the person chose; the rest stay as they are.
            checks = [c for c in checks if c.name in chosen]
        if not checks:
            view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
            return PlaybookResult(
                result={
                    "skipped": {
                        "gated": f"CI has not run: the {view.label!r} label is missing",
                        "stopped": "CI stopped at its gate before the label came; "
                        "a new run starts it",
                        "approval": "CI waits for a maintainer to approve running it",
                        "running": "CI is still running",
                        "passed": "CI passed",
                        "none": "no CI ran at this commit",
                    }.get(view.state, "nothing failed"),
                }
            )
        team = await ctx.author_is_team(pr)
        ctx.trusted_input = team
        hints = ActionHints(author_is_team=team, changed_paths=pr.paths)
        failures = await first_looks(ctx, pr, profile, checks)
        actions, signals = [], []
        reruns: set[int] = set()
        deferred: set[int] = set()
        for failure in failures:
            check, diagnosis, sig = failure.check, failure.diagnosis, failure.sig
            if (
                diagnosis.category == "flaky"
                and check.run_id
                and check.run_id not in reruns
            ):
                done = await reruns_at_head(ctx, pr, check.run_id)
                if done >= profile.ci.rerun.max_per_sha:
                    # Failing after every rerun: no longer treated as a flake.
                    failure.reruns = done
                elif await self._backoff_elapsed(
                    ctx, pr, check.run_id, done, profile.ci.rerun.backoff_seconds
                ):
                    reruns.add(check.run_id)
                    action = make_action(
                        ActionKind.CI_RERUN_FAILED,
                        pr.repo,
                        RerunPayload(
                            run_id=check.run_id,
                            workflow=check.workflow,
                            attempt=check.run_attempt,
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=f"{check.run_id}:{done}",
                        rationale=f"{check.name}: {diagnosis.explanation}",
                        hints=hints,
                    )
                    action.provenance.requested_by = by
                    actions.append(action)
                else:
                    deferred.add(check.run_id)
            if diagnosis.signature and sig is None:
                signals.append(
                    Signal(
                        kind=SignalKind.CI_OUTCOME,
                        repo=pr.repo,
                        number=pr.number,
                        payload={
                            "suggested_signature": diagnosis.signature.model_dump(),
                            "check": check.name,
                            "category": diagnosis.category,
                        },
                    )
                )
        causes, base = await find_causes(ctx, pr, profile, failures)
        # What the first look could not call a flake but the root cause lays on the
        # runner or a flake is run again too, within the same budget.
        by_job = {job: c for c in causes or [] for job in c.jobs}
        for f in failures:
            cause = by_job.get(f.check.name)
            run = f.check.run_id
            if (
                cause is None
                or cause.origin not in ("infra", "flaky")
                or not run
                or run in reruns
                or f.diagnosis.category in ("flaky", "infra")
            ):
                continue
            f.diagnosis = f.diagnosis.model_copy(
                update={"category": cause.origin, "explanation": cause.cause}
            )
            done = await reruns_at_head(ctx, pr, run)
            if done >= profile.ci.rerun.max_per_sha:
                continue
            reruns.add(run)
            action = make_action(
                ActionKind.CI_RERUN_FAILED,
                pr.repo,
                RerunPayload(
                    run_id=run, workflow=f.check.workflow, attempt=f.check.run_attempt
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"{run}:{done}",
                rationale=f"{f.check.name}: {cause.cause}",
                hints=hints,
            )
            action.provenance.requested_by = by
            actions.append(action)
        await record(ctx, pr, failures, causes)
        lines = explained(failures, causes, base)
        for cause in causes or []:
            if cause.origin != "change":
                kind = cause.origin if cause.origin in ("infra", "base") else "unknown"
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        pr.repo,
                        NotifyPayload(
                            event=f"ci.{kind}",
                            text=f"{pr.repo}#{pr.number} {', '.join(cause.jobs)}: "
                            f"{cause.failing}: {cause.cause}",
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=f"cause:{','.join(sorted(cause.jobs))}",
                    )
                )
        by_job = {job for cause in causes or [] for job in cause.jobs}
        for f in failures:
            if (
                f.real
                and f.check.name not in by_job
                and not f.reruns
                and f.diagnosis.category in ("infra", "unknown")
            ):
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        pr.repo,
                        NotifyPayload(
                            event=f"ci.{f.diagnosis.category}",
                            text=f"{pr.repo}#{pr.number} {f.check.name}: "
                            f"{f.diagnosis.explanation}",
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=str(f.check.id),
                    )
                )
        if lines:
            body = f"### CI failures at `{short_sha(pr.head_sha)}`\n\n" + "\n".join(
                lines
            )
            action = make_action(
                ActionKind.COMMENT_UPSERT,
                pr.repo,
                CommentUpsertPayload(purpose="ci", body=body),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=",".join(sorted(str(c.id) for c in checks)),
                hints=hints,
            )
            action.provenance.requested_by = by
            actions.append(action)
        result = {
            "reruns": sorted(reruns),
            "deferred": sorted(deferred),
            "explained": len(lines),
            "root_causes": len(causes or []),
        }
        if deferred:
            return PlaybookResult(
                actions=actions,
                signals=signals,
                retry_after=profile.ci.rerun.backoff_seconds,
                result=result,
            )
        return PlaybookResult(actions=actions, signals=signals, result=result)

    async def _backoff_elapsed(
        self, ctx: PlaybookContext, pr: PrSnapshot, run_id: int, done: int, backoff: int
    ) -> bool:
        """The first rerun goes at once; later ones wait ``backoff`` seconds after the failure is seen."""
        if done == 0 or backoff <= 0:
            return True
        key = f"ci_backoff:{pr.repo}#{pr.number}@{pr.head_sha[:12]}:{run_id}:{done}"
        seen = await ctx.store.get_state(key)
        now = ctx.clock.now()
        if seen is None:
            await ctx.store.set_state(key, now.isoformat())
            return False
        from datetime import datetime

        return (now - datetime.fromisoformat(seen)).total_seconds() >= backoff

    async def _manual_rerun(
        self, ctx: PlaybookContext, pr: PrSnapshot, event: Event
    ) -> PlaybookResult:
        author = event.payload.get("author", "")
        allowed = await ctx.may_command(author)
        if not allowed and author.lower() == pr.author.lower():
            earlier = await ctx.store.list_actions(
                repo=pr.repo, number=pr.number, kinds=[ActionKind.CI_RERUN_FAILED]
            )
            allowed = not any(
                r.action.provenance.requested_by == author
                and same_commit(r.action.head_sha, pr.head_sha)
                for r in earlier
            )
        if not allowed:
            return PlaybookResult(result={"skipped": f"{author} may not rerun CI here"})
        runs = sorted({c.run_id for c in pr.checks if c.failed and c.run_id})
        actions = []
        for run_id in runs:
            action = make_action(
                ActionKind.CI_RERUN_FAILED,
                pr.repo,
                RerunPayload(run_id=run_id),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"manual:{event.payload.get('comment_id')}:{run_id}",
                rationale=f"/rerun by {author}",
                hints=ActionHints(author_is_team=await ctx.author_is_team(pr)),
            )
            action.provenance.requested_by = author
            actions.append(action)
        return PlaybookResult(actions=actions, result={"runs": runs})


async def _root_cause(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    failures: list[Failure],
    base: str,
) -> list[RootCause] | None:
    """Ask an agent, in a read-only checkout of the PR head, why these jobs fail;
    ``None`` when it gives no usable answer (the first look then stands)."""
    workdir = await ctx.workspace.prepare(pr.repo, pr.number, pr.head_sha, base)
    try:
        diff = without_generated(
            await ctx.workspace.diff(workdir, f"origin/{base}", pr.head_sha)
        )
        logs = [
            untrusted(
                f"ci.log {f.check.name}",
                f"Job: {f.check.name} (workflow {f.check.workflow}, run "
                f"{f.check.run_id})\nFirst look: {f.diagnosis.category}: "
                f"{f.diagnosis.explanation}"
                + (f"\nIt failed again after {f.reruns} reruns." if f.reruns else "")
                + f"\n\n{excerpt(f.log)}",
                30_000,
            )
            for f in failures
        ]
        prompt = "\n\n".join(
            [
                "Find the root cause of these CI failures. The working directory is "
                f"a checkout of the PR head {pr.head_sha}; compare against "
                f"origin/{base}. Read the failing tests and the code they exercise "
                "until you can name the mechanism, not only the symptom. Several jobs "
                "often fail for one reason: return one cause per distinct reason, "
                "listing the jobs it explains. For each, say what fails, why, where "
                "(files and lines; for a cause in this change, the changed lines), "
                "and the smallest fix. Origin `change` when this PR's change causes "
                "it; `base` when it does not depend on the change (the failing code "
                "and test are untouched by the diff, or the log shows the base "
                "branch fails the same way); `infra` for the runner or the CI "
                "service; `flaky` for nondeterminism; `unclear` when the evidence "
                "does not settle it. Do not guess: say `unclear` and what is missing. "
                "The logs below hold what each job printed around its failure: work "
                "from them and the checkout. Fetch a job's log only when its excerpt "
                "cuts off the failure, and never other runs, workflows, or the base "
                "branch's history; stop reading once you can name the mechanism.",
                work_lessons_block(await ctx.knowledge.agent_lessons(pr.repo)),
                *logs,
                pr_block(pr, diff),
                schema_hint(RootCauseReport.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.CI_ROOT_CAUSE,
            prompt=prompt,
            workdir=workdir,
            output_schema=RootCauseReport.model_json_schema(),
            skill_dirs=ctx.skill_dirs(),
            repo=pr.repo,
        )
        answer = await ctx.ask(task, RootCauseReport)
        if not answer.ok or answer.structured is None:
            return None
        report = RootCauseReport.model_validate(answer.structured)
        causes = accepted_causes(
            report, [f.check.name for f in failures], pr.paths, Path(workdir)
        )
        return causes or None
    finally:
        await ctx.workspace.release(workdir)


async def _diagnose_all(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    logs: list[tuple[CheckRun, str]],
    learned: str = "",
) -> dict[str, Diagnosis]:
    """The first look at each of ``logs``, by job name: one agent call for them
    all, since most of a call's tokens go to the agent's own setup."""
    if not logs:
        return {}
    if len(logs) == 1:
        check, log = logs[0]
        one = await _diagnose(ctx, pr, check, log, learned)
        return {check.name: one} if one is not None else {}
    each = max(8_000, 60_000 // len(logs))
    prompt = "\n\n".join(
        [
            "Classify each of these failed CI jobs of one run, one entry per job "
            "under its name. `flaky`: transient (network, timeouts, nondeterminism "
            "unrelated to the change). `infra`: the runner or CI system failed. "
            "`pr_caused`: the change broke a build or test. `unknown`: not enough "
            "evidence. Suggest a regex signature only for flaky or infra failures "
            f"that will recur. {FROM_THE_LOG}",
            learned,
            *(
                untrusted(
                    f"ci.log {check.name}",
                    f"Job: {check.name} (workflow {check.workflow}, run "
                    f"{check.run_id})\n\n{log}",
                    each,
                )
                for check, log in logs
            ),
            pr_block(pr),
            schema_hint(Diagnoses.model_json_schema()),
        ]
    )
    task = AgentTask(
        kind=TaskKind.CI_DIAGNOSE,
        prompt=prompt,
        workdir=ctx.scratch(),
        output_schema=Diagnoses.model_json_schema(),
        repo=pr.repo,
        budget="quick",
    )
    answer = await ctx.ask(task, Diagnoses)
    if not answer.ok or answer.structured is None:
        return {}
    names = {check.name for check, _ in logs}
    return {
        d.job: Diagnosis.model_validate(d.model_dump(exclude={"job"}))
        for d in Diagnoses.model_validate(answer.structured).jobs
        if d.job in names
    }


async def _diagnose(
    ctx: PlaybookContext, pr: PrSnapshot, check: CheckRun, log: str, learned: str = ""
) -> Diagnosis | None:
    workdir = ctx.scratch()
    prompt = "\n\n".join(
        [
            "Classify a failed CI job. `flaky`: transient (network, timeouts, nondeterminism unrelated to the change). "
            "`infra`: the runner or CI system failed. `pr_caused`: the change broke a build or test. "
            "`unknown`: not enough evidence. Suggest a regex signature only for flaky or infra failures that will recur. "
            + FROM_THE_LOG,
            learned,
            f"Job: {check.name} (workflow {check.workflow}, run {check.run_id})",
            untrusted("ci.log", log, 40_000),
            pr_block(pr),
            schema_hint(Diagnosis.model_json_schema()),
        ]
    )
    task = AgentTask(
        kind=TaskKind.CI_DIAGNOSE,
        prompt=prompt,
        workdir=workdir,
        output_schema=Diagnosis.model_json_schema(),
        repo=pr.repo,
        budget="quick",
    )
    answer = await ctx.ask(task, Diagnosis)
    if not answer.ok or answer.structured is None:
        return None
    return Diagnosis.model_validate(answer.structured)
