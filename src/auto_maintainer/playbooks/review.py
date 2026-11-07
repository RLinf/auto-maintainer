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

"""First review (design 5.5): the profile's review skill, headless, then a verifier pass."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionHints,
    ActionKind,
    ActionStatus,
    LabelPayload,
    LabReproducePayload,
    RequestReviewersPayload,
    ReviewPostPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import short_sha
from ..domain.ports import Store
from ..domain.profile import RepoProfile
from ..domain.records import JobStatus
from ..domain.review import (
    DismissedFinding,
    ReviewResult,
    ReviewState,
    classify_review_state,
    render_review,
)
from ..domain.snapshot import PrState
from .base import PR_EVENTS, Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import (
    accepted_labels,
    headless,
    interactive,
    label_block,
    label_choices,
    pr_block,
    reproduction_block,
    review_skill_block,
    schema_hint,
    skill_format_block,
    skill_owns_format,
    template_block,
    untrusted,
)
from .requests import FOLLOWUP_STATES, review_key, review_request, starts_review


class Verdict(BaseModel):
    id: str
    confirmed: bool
    reason: str = ""


class VerifyAnswer(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


class ReviewPlaybook(Playbook):
    name = "review"
    triggers = (*PR_EVENTS, EventKind.DASHBOARD_COMMAND, EventKind.PR_COMMENT)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return starts_review(event, profile)

    def key(self, event: Event) -> str:
        return review_key(self.name, event)

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        request = review_request(event)
        if request is not None and request.manual:
            ctx.requested_by = request.by
            ctx.agent = request.agent
        manual = request is not None and request.manual
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if not manual and not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        if (
            request is not None
            and request.source == "comment"
            and not await ctx.may_command(request.by or "")
        ):
            return PlaybookResult(
                result={"skipped": "the commenter may not request reviews"}
            )
        ctx.trusted_input = await ctx.author_is_team(pr)
        state = classify_review_state(
            pr,
            ctx.identities(),
            profile.comments.legacy_markers,
            profile.scope.include_drafts or manual,
            own_actions=ctx.own_reviews,
        ).state
        if state in FOLLOWUP_STATES:
            return PlaybookResult(
                result={
                    "skipped": "already reviewed; the follow-up handles new commits"
                }
            )
        if state == ReviewState.UP_TO_DATE:
            return PlaybookResult(result={"skipped": "already reviewed at this head"})
        if state != ReviewState.NEW_REVIEW and not (
            manual and state == ReviewState.SELF_PR
        ):
            return PlaybookResult(result={"skipped": f"state is {state}"})
        if not manual and not profile.scope.backfill_reviews:
            record = await ctx.store.get_repo(pr.repo)
            if record and pr.created_at and pr.created_at < record.added_at:
                return PlaybookResult(
                    result={
                        "skipped": "opened before the repository was added; ask for a review to run one"
                    }
                )

        info = await ctx.forge.get_repo(pr.repo)
        choices = label_choices(info.labels, profile, pr)
        limit = profile.review.max_suggested_labels
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
            own_format = skill_owns_format(profile, workdir)
            prompt = "\n\n".join(
                [
                    interactive(profile, ctx.requested_by)
                    if ctx.requested_by
                    else headless(profile),
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    skill_format_block(profile)
                    if own_format
                    else template_block(profile),
                    reproduction_block(
                        await reproduction_record(ctx.store, pr.repo, pr.number)
                    ),
                    f"Review lessons from this repository's maintainers:\n{lessons}"
                    if lessons
                    else "",
                    f"The working directory is a checkout of the PR head {pr.head_sha}. "
                    f"Compare against origin/{info.default_branch}.",
                    pr_block(pr, diff),
                    label_block(choices, limit),
                    schema_hint(ReviewResult.model_json_schema()),
                ]
            )
            task = AgentTask(
                kind=TaskKind.REVIEW,
                prompt=prompt,
                workdir=workdir,
                output_schema=ReviewResult.model_json_schema(),
                skill_dirs=ctx.skill_dirs(),
                repo=pr.repo,
            )
            answer = await ctx.ask(task, ReviewResult)
            if not answer.ok or answer.structured is None:
                raise RuntimeError(f"review agent failed: {answer.error}")
            result = ReviewResult.model_validate(answer.structured)
            runtime = answer.runtime
            if profile.review.verify and result.findings:
                result = await self._verify(ctx, result, pr_block(pr, diff), workdir)
        finally:
            await ctx.workspace.release(workdir)

        result.labels = accepted_labels(result.labels, choices, limit)
        meaning = profile.review.meaning_of(result.recommendation)
        if meaning is None:
            meaning = (
                "changes"
                if any(f.severity in ("critical", "major") for f in result.findings)
                else "ready"
            )
            result.recommendation = profile.review.recommendations[meaning]
        rendered = render_review(result, profile, set(pr.paths), own_format=own_format)
        team = ctx.trusted_input
        hints = ActionHints(
            author_is_team=team, recommendation=meaning, changed_paths=pr.paths
        )
        actions = [
            make_action(
                ActionKind.REVIEW_POST,
                pr.repo,
                ReviewPostPayload(
                    body=rendered.body,
                    comments=rendered.comments,
                    recommendation=result.recommendation,
                    runtime=runtime,
                    kind="review",
                    result=result.model_dump(mode="json"),
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"req:{event.id}" if manual else "",
                rationale=f"first review at {short_sha(pr.head_sha)}: {len(result.findings)} findings, {result.recommendation}",
                hints=hints,
                draft_for=request.by if request else None,
            )
        ]
        if meaning == "changes":
            actions.append(
                make_action(
                    ActionKind.LABEL_APPLY,
                    pr.repo,
                    LabelPayload(
                        roles=["changes_requested"], remove_roles=["needs_review"]
                    ),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator="review",
                    hints=hints,
                    draft_for=request.by if request else None,
                )
            )
        elif meaning == "ready":
            if profile.label_for("ci_gate"):
                actions.append(
                    make_action(
                        ActionKind.LABEL_CI_GATE,
                        pr.repo,
                        LabelPayload(roles=["ci_gate"]),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator="review",
                        hints=hints,
                        draft_for=request.by if request else None,
                    )
                )
            owners = [
                o
                for o in await ctx.codeowners_for(pr.paths)
                if "/" not in o and o.lower() != pr.author.lower()
            ]
            if owners:
                actions.append(
                    make_action(
                        ActionKind.PR_REQUEST_REVIEWERS,
                        pr.repo,
                        RequestReviewersPayload(logins=owners[:3]),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        hints=hints,
                        draft_for=request.by if request else None,
                    )
                )
        if result.labels:
            actions.append(
                make_action(
                    ActionKind.LABEL_APPLY,
                    pr.repo,
                    LabelPayload(labels=result.labels),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator=f"req:{event.id}:labels"
                    if manual
                    else "review:labels",
                    rationale=f"labels the review suggests: {', '.join(result.labels)}",
                    hints=hints,
                    draft_for=request.by if request else None,
                )
            )
        return PlaybookResult(
            actions=actions,
            result={
                "findings": len(result.findings),
                "dismissed": len(result.dismissed),
                "recommendation": result.recommendation,
                "labels": result.labels,
            },
        )

    async def _verify(
        self, ctx: PlaybookContext, result: ReviewResult, context: str, workdir: object
    ) -> ReviewResult:
        findings = "\n\n".join(
            f"{f.id} [{f.severity}/{f.category}] {f.title}\n{f.path}:{f.line}\n{f.body}"
            for f in result.findings
        )
        prompt = "\n\n".join(
            [
                "You verify review findings. For each finding, check the code in the working directory "
                "and decide whether it is real. Confirm only findings you can verify; give a one-line reason.",
                untrusted("review.findings", findings, 40_000),
                context,
                schema_hint(VerifyAnswer.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.VERIFY,
            prompt=prompt,
            workdir=workdir,
            output_schema=VerifyAnswer.model_json_schema(),
        )
        answer = await ctx.ask(task, VerifyAnswer)
        if not answer.ok or answer.structured is None:
            return result
        verdicts = {
            v.id: v for v in VerifyAnswer.model_validate(answer.structured).verdicts
        }
        kept, dismissed = [], list(result.dismissed)
        for f in result.findings:
            verdict = verdicts.get(f.id)
            if verdict is None or verdict.confirmed:
                kept.append(f.model_copy(update={"verified": verdict is not None}))
            else:
                dismissed.append(
                    DismissedFinding(
                        title=f.title,
                        reason=verdict.reason or "not confirmed by the verifier",
                    )
                )
        return result.model_copy(update={"findings": kept, "dismissed": dismissed})


async def reproduction_record(store: Store, repo: str, number: int) -> str | None:
    """The PR's latest reproduction on a test machine that ran: the approved plan,
    where and at which commits it ran, and what the service concluded from the runs.
    Run output comes from the PR's code, so it is marked untrusted."""
    runs = [
        r
        for r in await store.list_actions(
            repo=repo, number=number, kinds=[ActionKind.LAB_REPRODUCE], limit=50
        )
        if r.status == ActionStatus.EXECUTED and r.external_ref.get("runs")
    ]
    if not runs:
        return None
    record = max(runs, key=lambda r: r.updated_at)
    plan = LabReproducePayload.model_validate(record.action.payload)
    reports = [
        job
        for job in await store.list_jobs([JobStatus.DONE], repo=repo, limit=500)
        if job.playbook == "reproduce"
        and job.number == number
        and (job.result or {}).get("conclusion")
    ]
    conclusion: dict[str, Any] = (
        max(reports, key=lambda j: j.updated_at).result or {} if reports else {}
    )
    summary = {
        "machine": plan.lab,
        "approved_by": record.approver,
        "head_sha": record.action.head_sha,
        "plan": plan.summary,
        "setup": plan.setup,
        "script": plan.script,
        "expect": plan.expect,
        "runs": {
            side: {k: v for k, v in run.items() if k != "tail"}
            for side, run in (record.external_ref.get("runs") or {}).items()
        },
        "conclusion": conclusion.get("conclusion"),
        "notes": conclusion.get("notes"),
    }
    return untrusted(
        "reproduction", json.dumps(summary, ensure_ascii=False, indent=1), 20_000
    )
