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

"""Priority evaluation (design 5.6). Internal only: produces no public action."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.scoring import (
    DEFAULT_RUBRIC,
    Evaluation,
    Rubric,
    missing_dimensions,
    recompute,
)
from ..domain.snapshot import PrSnapshot
from .base import PR_EVENTS, Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import pr_block, schema_hint


class EvalAnswer(BaseModel):
    scores: dict[str, float] = Field(
        description="Dimension name to a score from 0 to 10."
    )
    summary: str = Field(
        description="One sentence on what the PR does, in the internal language."
    )
    rationale: str = Field(
        description="Short reasons for the scores, in the internal language."
    )


def rubric_of(profile: RepoProfile) -> Rubric:
    rubric = profile.priority.rubric
    return rubric if isinstance(rubric, Rubric) else DEFAULT_RUBRIC


def haystack(pr: PrSnapshot, evaluation: Evaluation, state: str) -> str:
    return " ".join([pr.title, evaluation.summary, state, " ".join(pr.labels)])


def material_change(
    pr: PrSnapshot, evaluation: Evaluation, previous: PrSnapshot | None
) -> bool:
    """Whether a PR changed enough since it was scored to score it again."""
    if not evaluation.scores:
        return True
    if previous is None:
        return False
    size_then = previous.additions + previous.deletions
    size_now = pr.additions + pr.deletions
    grew = size_then > 0 and size_now > 2 * size_then and size_now - size_then > 200
    return grew or pr.title != previous.title


def scored_elsewhere(evaluation: Evaluation, rubric: Rubric) -> bool:
    """Whether ``evaluation`` was scored on dimensions ``rubric`` does not have, so a
    score kept under a shared name may mean something else: score it again in full."""
    return any(name not in rubric.dimensions for name in evaluation.scores)


class EvaluatePlaybook(Playbook):
    """Scores a PR when it opens or changes materially, and again on request: a
    maintainer's ``evaluate`` command scores every dimension afresh, with the
    repository's lessons as they are now. A rubric change recomputes every total; a PR
    scored on other dimensions keeps its last total until an agent scores it again."""

    name = "evaluate"
    triggers = (
        *PR_EVENTS,
        EventKind.MEMORY_CHANGED,
        EventKind.DASHBOARD_COMMAND,
        EventKind.PRIORITY_RESCORE,
    )
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if profile is None or not profile.priority.enabled:
            return False
        if event.kind == EventKind.DASHBOARD_COMMAND:
            return (
                event.payload.get("command") == "evaluate" and event.number is not None
            )
        if event.kind == EventKind.MEMORY_CHANGED:
            return event.repo is not None and any(
                "rubric" in p or "profile" in p for p in event.payload.get("paths", [])
            )
        if event.kind == EventKind.PRIORITY_RESCORE:
            return event.number is not None
        return True

    def key(self, event: Event) -> str:
        if event.kind == EventKind.DASHBOARD_COMMAND:
            return f"evaluate:{event.repo}#{event.number}:manual:{event.id}"
        if event.kind == EventKind.MEMORY_CHANGED:
            return f"evaluate:{event.repo}:memory:{event.payload.get('sha')}"
        if event.kind == EventKind.PRIORITY_RESCORE:
            return f"evaluate:{event.repo}#{event.number}:rubric:{event.payload.get('sha')}"
        return f"evaluate:{event.repo}#{event.number}@{(event.sha or '-')[:12]}:{event.kind}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo
        rubric = rubric_of(profile)
        if event.kind == EventKind.MEMORY_CHANGED:
            return await self._rescore_all(ctx, profile, rubric, event)
        assert event.number
        if event.kind == EventKind.DASHBOARD_COMMAND and event.payload.get("by"):
            # Someone pressed Score again: theirs, on their own agent when they run
            # one, and its questions go to them.
            ctx.requested_by = str(event.payload["by"])
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        ctx.trusted_input = await ctx.author_is_team(pr)
        state = ctx.classify(pr).state
        evaluation = await ctx.store.get_evaluation(pr.repo, pr.number) or Evaluation(
            repo=pr.repo, number=pr.number
        )
        previous = await ctx.store.get_snapshot(pr.repo, pr.number)
        need = material_change(
            pr,
            evaluation,
            previous if previous and previous.head_sha != pr.head_sha else None,
        )
        need = need or bool(missing_dimensions(evaluation, rubric))
        forced = event.kind in (
            EventKind.DASHBOARD_COMMAND,
            EventKind.PRIORITY_RESCORE,
        ) or scored_elsewhere(evaluation, rubric)
        scored = False
        if need or forced:
            answer = await self._ask(
                ctx,
                pr,
                rubric,
                profile,
                missing_dimensions(evaluation, rubric)
                if evaluation.scores and not forced
                else None,
            )
            if answer is not None:
                scores = {
                    **{
                        k: v
                        for k, v in evaluation.scores.items()
                        if k in rubric.dimensions
                    },
                    **{
                        k: max(0.0, min(10.0, v))
                        for k, v in answer.scores.items()
                        if k in rubric.dimensions
                    },
                }
                evaluation = evaluation.model_copy(
                    update={
                        "scores": scores,
                        "summary": evaluation.summary or answer.summary,
                        "rationale": answer.rationale,
                        "evaluated_at": ctx.clock.now(),
                        "evaluated_by": "agent",
                        "scored_sha": pr.head_sha,
                    }
                )
                scored = True
        goals = await ctx.knowledge.stage_goals(pr.repo, profile)
        evaluation = recompute(
            evaluation.model_copy(update={"title": pr.title}),
            rubric,
            goals,
            haystack(pr, evaluation, state),
            ctx.clock.now(),
        )
        await ctx.store.save_evaluation(evaluation)
        return PlaybookResult(
            result={
                "tier": evaluation.tier,
                "total": evaluation.total,
                "scored": scored,
            }
        )

    async def _rescore_all(
        self, ctx: PlaybookContext, profile: RepoProfile, rubric: Rubric, event: Event
    ) -> PlaybookResult:
        """Recompute each open PR's total under the current rubric and stage goals. A
        PR without a score for every dimension is not recomputed, since the missing
        ones would count as zero: an agent scores it again in full."""
        goals = await ctx.knowledge.stage_goals(profile.repo, profile)
        count = 0
        again: list[Event] = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
            if evaluation is None:
                continue
            if missing_dimensions(evaluation, rubric):
                again.append(
                    Event(
                        kind=EventKind.PRIORITY_RESCORE,
                        repo=pr.repo,
                        number=pr.number,
                        payload={
                            "sha": event.payload.get("sha"),
                            "batch": f"rubric:{event.payload.get('sha')}",
                        },
                    )
                )
                continue
            state = ctx.classify(pr).state
            await ctx.store.save_evaluation(
                recompute(
                    evaluation,
                    rubric,
                    goals,
                    haystack(pr, evaluation, state),
                    ctx.clock.now(),
                )
            )
            count += 1
        return PlaybookResult(
            events=again, result={"rescored": count, "scoring_again": len(again)}
        )

    async def _ask(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        rubric: Rubric,
        profile: RepoProfile,
        only: list[str] | None,
    ) -> EvalAnswer | None:
        dims = {k: v for k, v in rubric.dimensions.items() if not only or k in only}
        rules = "\n".join(
            f"- {name} (weight {d.weight:.2f}): {d.rule}" for name, d in dims.items()
        )
        workdir = ctx.scratch()
        lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
        prompt = "\n\n".join(
            [
                "Score this pull request for the maintainers' private review queue. "
                "Give each dimension a score from 0 to 10 using its rule. This evaluation is never shown publicly. "
                "Judge only from what is given here: do not run commands, read files, or fetch anything.",
                f"Dimensions:\n{rules}",
                *(
                    [
                        "Lessons from this repository's maintainers; apply those about "
                        f"priority when you score:\n{lessons}"
                    ]
                    if lessons
                    else []
                ),
                f"Write the summary and rationale in {profile.languages.internal}.",
                pr_block(pr),
                schema_hint(EvalAnswer.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.EVALUATE,
            prompt=prompt,
            workdir=workdir,
            output_schema=EvalAnswer.model_json_schema(),
            repo=pr.repo,
        )
        result = await ctx.ask(task, EvalAnswer)
        if not result.ok or result.structured is None:
            return None
        return EvalAnswer.model_validate(result.structured)
