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

"""CI doctor (design 5.5): classify failed jobs, rerun flakes, explain real failures."""

from __future__ import annotations

import re
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
from ..domain.records import CiObservation
from ..domain.snapshot import CheckConclusion, CheckRun, PrSnapshot
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import pr_block, schema_hint, untrusted

_ERROR_RE = re.compile(
    r"(error|failed|failure|exception|traceback|assert|fatal)", re.IGNORECASE
)


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


def _rerun_requested(event: Event) -> bool:
    return event.kind == EventKind.PR_COMMENT and any(
        c.get("name") == "rerun" for c in event.payload.get("commands", [])
    )


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
        explained: list[str] = []
        for check in checks:
            log = await ctx.ci.get_job_log(pr.repo, check.id) or check.summary or ""
            sig = match_signature(log, check, signatures)
            if sig is not None:
                diagnosis = Diagnosis(
                    category=sig.category,
                    confidence=0.9,
                    explanation=sig.description or f"matched {sig.id}",
                )
            else:
                diagnosis = await self._diagnose(
                    ctx, pr, check, excerpt(log)
                ) or Diagnosis(
                    category="unknown", confidence=0.0, explanation="no diagnosis"
                )
            await ctx.store.add_ci_observation(
                CiObservation(
                    repo=pr.repo,
                    number=pr.number,
                    sha=pr.head_sha,
                    run_id=check.run_id,
                    check_id=check.id,
                    check_name=check.name,
                    workflow=check.workflow,
                    category=diagnosis.category,
                    signature=sig.id if sig else None,
                    confidence=diagnosis.confidence,
                    explanation=diagnosis.explanation,
                    rerun=diagnosis.category == "flaky",
                )
            )
            if (
                diagnosis.category == "flaky"
                and check.run_id
                and check.run_id not in reruns
            ):
                done = await self._reruns(ctx, pr, check.run_id)
                if done >= profile.ci.rerun.max_per_sha:
                    explained.append(
                        f"- **{check.name}** failed again after {done} reruns: {diagnosis.explanation}"
                    )
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

    async def _diagnose(
        self, ctx: PlaybookContext, pr: PrSnapshot, check: CheckRun, log: str
    ) -> Diagnosis | None:
        workdir = ctx.service.scratch_dir / ctx.job.id
        workdir.mkdir(parents=True, exist_ok=True)
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
