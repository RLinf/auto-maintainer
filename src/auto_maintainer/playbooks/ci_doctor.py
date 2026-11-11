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
of real failures.

Each failed job gets a first look from its log: a flake signature, the first look at
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
from ..domain.snapshot import CheckConclusion, CheckRun, PrSnapshot
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import pr_block, schema_hint, untrusted, without_generated

_ERROR_RE = re.compile(
    r"(error|failed|failure|exception|traceback|assert|fatal)", re.IGNORECASE
)
_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*")
_VOLATILE_RE = re.compile(
    r"0x[0-9a-f]+|\b[0-9a-f]{7,40}\b|/tmp/\S+|\d+(\.\d+)?", re.IGNORECASE
)
REUSE_DAYS = 7
"""How long a flake's or a runner failure's first look stands for the same failure."""


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
class _Failure:
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


class CiDoctor(Playbook):
    name = "ci_doctor"
    triggers = (EventKind.CHECKS_COMPLETED, EventKind.PR_COMMENT)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if event.kind == EventKind.PR_COMMENT:
            return _rerun_requested(event)
        return bool(event.payload.get("failed"))

    def key(self, event: Event) -> str:
        if event.kind == EventKind.PR_COMMENT:
            return f"ci_doctor:{event.repo}#{event.number}:rerun:{event.payload.get('comment_id')}"
        failed = ",".join(str(i) for i in sorted(event.payload.get("failed", [])))
        return (
            f"ci_doctor:{event.repo}#{event.number}@{(event.sha or '-')[:12]}:{failed}"
        )

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        if event.kind == EventKind.PR_COMMENT:
            return await self._manual_rerun(ctx, pr, event)
        if event.sha and not same_commit(event.sha, pr.head_sha):
            return PlaybookResult(result={"skipped": "the head moved on"})
        failed_ids = set(event.payload.get("failed", []))
        checks = [c for c in pr.checks if c.id in failed_ids and c.failed]
        if not profile.ci.cancelled_is_failure:
            checks = [c for c in checks if c.conclusion != CheckConclusion.CANCELLED]
        signatures = await ctx.knowledge.flake_signatures(pr.repo, profile)
        team = await ctx.author_is_team(pr)
        ctx.trusted_input = team
        hints = ActionHints(author_is_team=team, changed_paths=pr.paths)
        actions, signals = [], []
        reruns: set[int] = set()
        deferred: set[int] = set()
        failures: list[_Failure] = []
        seen = await ctx.store.list_ci_observations(
            pr.repo, since=ctx.clock.now() - timedelta(days=REUSE_DAYS)
        )
        new: list[tuple[CheckRun, str, FlakeSignature | None, str | None]] = []
        for check in checks:
            log = await ctx.ci.get_job_log(pr.repo, check.id) or check.summary or ""
            sig = match_signature(log, check, signatures)
            mark = fingerprint(log, check)
            before = _seen_before(seen, pr, check, mark) if sig is None else None
            if sig is not None:
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
                _Failure(
                    check,
                    sig,
                    diagnosis,
                    log,
                    fingerprint=mark,
                    cause=_same_head_cause(before, pr),
                )
            )
        found = await self._diagnose_all(
            ctx, pr, [(c, excerpt(log)) for c, log, _, _ in new]
        )
        for check, log, sig, mark in new:
            diagnosis = found.get(check.name) or Diagnosis(
                category="unknown", confidence=0.0, explanation="no diagnosis"
            )
            failures.append(_Failure(check, sig, diagnosis, log, fingerprint=mark))
        for failure in failures:
            check, diagnosis, sig = failure.check, failure.diagnosis, failure.sig
            if (
                diagnosis.category == "flaky"
                and check.run_id
                and check.run_id not in reruns
            ):
                done = await self._reruns(ctx, pr, check.run_id)
                if done >= profile.ci.rerun.max_per_sha:
                    # Failing after every rerun: no longer treated as a flake.
                    failure.reruns = done
                elif await self._backoff_elapsed(
                    ctx, pr, check.run_id, done, profile.ci.rerun.backoff_seconds
                ):
                    reruns.add(check.run_id)
                    actions.append(
                        make_action(
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
                    )
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
        real = [f for f in failures if f.real]
        causes: list[RootCause] | None = None
        base = "main"
        asked = [f for f in real if f.needs_cause] if profile.ci.root_cause else []
        known = [f.cause for f in real if f.cause is not None]
        if asked or known:
            base = (await ctx.forge.get_repo(pr.repo)).default_branch
        if asked:
            causes = await self._root_cause(ctx, pr, asked, base)
        if known:
            unique = {c.model_dump_json(): c for c in known}
            causes = [*(causes or []), *unique.values()]
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
                    category=f.diagnosis.category,
                    signature=f.sig.id if f.sig else None,
                    confidence=f.diagnosis.confidence,
                    explanation=f.diagnosis.explanation,
                    rerun=f.diagnosis.category == "flaky" and not f.reruns,
                    root_cause=by_job.get(f.check.name),
                    fingerprint=f.fingerprint,
                )
            )
        explained: list[str] = []
        for cause in causes or []:
            if cause.origin in ("change", "base"):
                explained.append(render_cause(cause, base))
            if cause.origin != "change":
                event = cause.origin if cause.origin in ("infra", "base") else "unknown"
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        pr.repo,
                        NotifyPayload(
                            event=f"ci.{event}",
                            text=f"{pr.repo}#{pr.number} {', '.join(cause.jobs)}: "
                            f"{cause.failing}: {cause.cause}",
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=f"cause:{','.join(sorted(cause.jobs))}",
                    )
                )
        for f in real:
            if f.check.name in by_job:
                continue
            # No root cause for this job (turned off, or the agent found none):
            # what the first look at the log said.
            check, diagnosis = f.check, f.diagnosis
            if f.reruns:
                explained.append(
                    f"- **{check.name}** failed again after {f.reruns} reruns: {diagnosis.explanation}"
                )
            elif diagnosis.category == "pr_caused":
                explained.append(f"- **{check.name}**: {diagnosis.explanation}")
            elif diagnosis.category in ("infra", "unknown"):
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        pr.repo,
                        NotifyPayload(
                            event=f"ci.{diagnosis.category}",
                            text=f"{pr.repo}#{pr.number} {check.name}: {diagnosis.explanation}",
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=str(check.id),
                    )
                )
        if explained:
            body = f"### CI failures at `{short_sha(pr.head_sha)}`\n\n" + "\n".join(
                explained
            )
            actions.append(
                make_action(
                    ActionKind.COMMENT_UPSERT,
                    pr.repo,
                    CommentUpsertPayload(purpose="ci", body=body),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator=",".join(sorted(str(c.id) for c in checks)),
                    hints=hints,
                )
            )
        result = {
            "reruns": sorted(reruns),
            "deferred": sorted(deferred),
            "explained": len(explained),
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

    async def _reruns(self, ctx: PlaybookContext, pr: PrSnapshot, run_id: int) -> int:
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
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        failures: list[_Failure],
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
                    + (
                        f"\nIt failed again after {f.reruns} reruns."
                        if f.reruns
                        else ""
                    )
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
                    "does not settle it. Do not guess: say `unclear` and what is missing.",
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
        self, ctx: PlaybookContext, pr: PrSnapshot, logs: list[tuple[CheckRun, str]]
    ) -> dict[str, Diagnosis]:
        """The first look at each of ``logs``, by job name: one agent call for them
        all, since most of a call's tokens go to the agent's own setup."""
        if not logs:
            return {}
        if len(logs) == 1:
            check, log = logs[0]
            one = await self._diagnose(ctx, pr, check, log)
            return {check.name: one} if one is not None else {}
        each = max(8_000, 60_000 // len(logs))
        prompt = "\n\n".join(
            [
                "Classify each of these failed CI jobs of one run, one entry per job "
                "under its name. `flaky`: transient (network, timeouts, nondeterminism "
                "unrelated to the change). `infra`: the runner or CI system failed. "
                "`pr_caused`: the change broke a build or test. `unknown`: not enough "
                "evidence. Suggest a regex signature only for flaky or infra failures "
                "that will recur.",
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
        self, ctx: PlaybookContext, pr: PrSnapshot, check: CheckRun, log: str
    ) -> Diagnosis | None:
        workdir = ctx.scratch()
        prompt = "\n\n".join(
            [
                "Classify a failed CI job. `flaky`: transient (network, timeouts, nondeterminism unrelated to the change). "
                "`infra`: the runner or CI system failed. `pr_caused`: the change broke a build or test. "
                "`unknown`: not enough evidence. Suggest a regex signature only for flaky or infra failures that will recur.",
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
        )
        answer = await ctx.ask(task, Diagnosis)
        if not answer.ok or answer.structured is None:
            return None
        return Diagnosis.model_validate(answer.structured)
