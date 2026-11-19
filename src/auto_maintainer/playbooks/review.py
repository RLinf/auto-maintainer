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

"""First review (design 5.5): the review skill, asked for the way a person asks for a
review, then its report put into the fields the service posts (``review_form``)."""

from __future__ import annotations

import json
from pathlib import Path
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
from ..domain.diffs import commentable
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
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
from ..domain.snapshot import PrSnapshot, PrState
from .base import (
    PR_EVENTS,
    Playbook,
    PlaybookContext,
    PlaybookResult,
    in_scope,
    keeps_session,
)
from .feedback import Rejection, declined_labels, rejections_block, rejections_on
from .person_notes import load_notes, notes_block
from .prompts import (
    NOTHING_RUNS,
    REVIEW_FORM,
    accepted_labels,
    checkout_line,
    forge_note,
    form_model,
    form_prompt,
    label_block,
    label_choices,
    own_review_skill,
    pr_block,
    quality_block,
    recommendations_line,
    review_call,
    review_instructions_block,
    review_rules,
    schema_hint,
    sections_rule,
    skill_owns_format,
    untrusted,
    without_generated,
    work_lessons_block,
    writing_block,
)
from .requests import (
    FOLLOWUP_STATES,
    ReviewRequest,
    recorded_review,
    replaced,
    review_key,
    review_request,
    settle_delay,
    starts_review,
)
from .review_form import fill_form, forget_report, keep_report, saved_report
from .review_session import (
    ReviewMachine,
    close,
    earlier_work,
    review_machine,
    session_task_fields,
    testing_block,
)


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

    async def run(self, ctx: PlaybookContext, wake: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        # The request that started the job; later events (its session opening) wake it.
        event = Event.model_validate(ctx.job.events[0]) if ctx.job.events else wake
        assert event.repo and event.number
        request = review_request(event)
        if request is not None and request.manual:
            ctx.requested_by = request.by
            ctx.agent = request.agent
        manual = request is not None and request.manual
        if not manual:
            delay = settle_delay(event, ctx.job.created_at, ctx.clock.now())
            if delay is not None:
                return PlaybookResult(
                    result={"waiting": "for more pushes to settle"}, retry_after=delay
                )
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if not manual and replaced(event, pr.head_sha):
            return PlaybookResult(
                result={"skipped": "a newer push replaced the commit"}
            )
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
        if (
            request is not None
            and (request.own or request.full)
            and state != ReviewState.SELF_PR
        ):
            # One of the PR's owners or reviewers wants a review of their own, or
            # someone asked for a full review: whoever reviewed it before.
            state = ReviewState.NEW_REVIEW
        elif not manual and state == ReviewState.NEW_REVIEW:
            # A review the service only recorded (nobody posted it) counts too, so the
            # next commit gets a follow-up rather than another full review.
            recorded = await recorded_review(ctx.store, pr.repo, pr.number)
            if recorded is not None:
                state = (
                    ReviewState.UP_TO_DATE
                    if same_commit(recorded.action.head_sha, pr.head_sha)
                    else ReviewState.FOLLOWUP_REVIEW
                )
        if state in FOLLOWUP_STATES:
            return PlaybookResult(
                result={
                    "skipped": "already reviewed; the follow-up handles new commits",
                    "handed_to": "followup",
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
        turned_down = await rejections_on(ctx.store, pr.repo, pr.number)
        declined = declined_labels(turned_down)
        choices = [
            c for c in label_choices(info.labels, profile, pr) if c not in declined
        ]
        limit = profile.review.max_suggested_labels
        # A report this job's agent wrote already needs only its form filled in.
        answer = await saved_report(ctx, pr.head_sha)
        machine = (
            ReviewMachine()
            if answer is not None
            else await review_machine(
                ctx,
                pr,
                profile,
                event.id,
                ctx.requested_by,
                timed_out=wake.kind == EventKind.JOB_TIMEOUT,
            )
        )
        if machine.wait is not None:
            return alongside(machine.wait, pr, request, ctx.job.id)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            diff = without_generated(
                await ctx.workspace.diff(
                    workdir, f"origin/{info.default_branch}", pr.head_sha
                )
            )
            own_format = skill_owns_format(profile, workdir)
            if answer is None:
                answer = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.REVIEW,
                        prompt=await review_prompt(
                            ctx,
                            pr,
                            profile,
                            info.default_branch,
                            workdir,
                            machine,
                            turned_down,
                        ),
                        workdir=workdir,
                        skill_dirs=ctx.skill_dirs(),
                        repo=pr.repo,
                        messages=True,
                        **session_task_fields(ctx, machine),
                    )
                )
                if not answer.ok:
                    raise RuntimeError(f"review agent failed: {answer.error}")
                await close(ctx, machine)
                await keep_report(ctx, pr.head_sha, answer)
            await ctx.keep_work_session(answer, workdir)
            form = await fill_form(
                ctx,
                answer,
                REVIEW_FORM_MODEL,
                form_prompt(
                    REVIEW_FORM.format(
                        sections=sections_rule(profile, own_format),
                        recommendations=recommendations_line(profile),
                    ),
                    answer.text,
                    [label_block(choices, limit), quality_block(profile)],
                ),
                workdir,
            )
            result = ReviewResult.model_validate(form.model_dump(mode="json"))
            runtime = answer.runtime
            if profile.review.verify and result.findings:
                result = await self._verify(ctx, result, pr_block(pr, diff), workdir)
        finally:
            await ctx.workspace.release(workdir)
            # A run the job's next try (or the next start) picks up keeps its session.
            if not keeps_session(ctx, answer):
                await close(ctx, machine)
        await forget_report(ctx)

        result.labels = accepted_labels(result.labels, choices, limit)
        meaning = profile.review.meaning_of(result.recommendation)
        if meaning is None:
            meaning = (
                "changes"
                if any(f.severity in ("critical", "major") for f in result.findings)
                else "ready"
            )
            result.recommendation = profile.review.recommendations[meaning]
        rendered = render_review(
            result,
            profile,
            set(pr.paths),
            own_format=own_format,
            ranges=commentable(diff),
        )
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
                **machine.summary(pr.head_sha),
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


REVIEW_FORM_MODEL = form_model(ReviewResult, "reproduction")
"""What the review's form turn fills: the review result, whose reproduction the
service starts alongside the review instead of from a brief."""


async def review_prompt(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    profile: RepoProfile,
    base_ref: str,
    workdir: Path,
    machine: ReviewMachine,
    turned_down: list[Rejection],
) -> str:
    """What the review agent reads: the skill to use on the PR, as a person asks for a
    review, then only what the skill cannot know: who it works for, the test machine,
    and what the maintainers taught or turned down."""
    if machine.token:
        testing = machine.testing(ctx, reproduce=ctx.requested_by is not None)
        earlier = await earlier_work(ctx, pr)
        testing = f"{testing}\n\n{earlier}" if earlier else testing
    else:
        testing = NOTHING_RUNS.format(
            note=machine.note or "No test machine is attached to this review."
        )
    lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
    return "\n\n".join(
        part
        for part in [
            review_call(profile, ctx.service.skills_dir, workdir, pr, "review"),
            checkout_line(pr.head_sha, base_ref),
            review_rules(profile, ctx.requested_by),
            testing,
            testing_block(await reproduction_record(ctx.store, pr.repo, pr.number)),
            review_instructions_block(profile),
            writing_block(profile, own_review_skill(profile, workdir) is not None),
            f"The maintainers' lessons for reviews of this repository:\n{lessons}"
            if lessons
            else "",
            notes_block(await load_notes(ctx.store, ctx.requested_by), "reviews")
            if ctx.requested_by
            else "",
            work_lessons_block(
                await ctx.knowledge.agent_lessons(
                    pr.repo, [machine.lab] if machine.lab else []
                )
            ),
            rejections_block(turned_down),
            forge_note(pr),
        ]
        if part
    )


def alongside(
    wait: PlaybookResult,
    pr: PrSnapshot,
    request: ReviewRequest | None,
    job_id: str,
) -> PlaybookResult:
    """The review's first wait for its session, which also starts the skill's
    reproduction agent on the same machine when someone asked for the review: it runs
    while the review does, and its record goes into this job's review
    (``reproduce.py``)."""
    lab = str(wait.actions[0].payload.get("lab") or "") if wait.actions else ""
    if not lab or request is None or not request.manual:
        # A review nobody asked for runs no reproduction beside it.
        return wait
    start = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=pr.repo,
        number=pr.number,
        sha=pr.head_sha,
        payload={
            "command": "reproduce",
            "by": request.by if request else None,
            "lab": lab,
            "alongside": job_id,
        },
        source="review",
    )
    return wait.model_copy(update={"events": [*wait.events, start]})


async def reproduction_record(store: Store, repo: str, number: int) -> str | None:
    """The PR's latest reproduction on a test machine that ran: its brief,
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
        for job in await store.list_jobs(
            [JobStatus.DONE],
            repo=repo,
            limit=500,
            playbooks=["reproduce"],
            number=number,
        )
        if (job.result or {}).get("conclusion")
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
