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

"""Reproduce what a PR claims on a test machine, without changing it (design 5.10).

Two requests start it. A maintainer asks from the PR (``dashboard.command`` with
``command: reproduce``), naming a machine; or a review asks once it is drafted
(``review.reproduce``), when the repository uses a machine and its ``lab.reproduce``
rule lets the PR run (``auto_reproduction``). In three steps, each from fresh reads:

1. An agent reads the PR in a checkout of its head and writes a reproduction that
   follows the PR's documentation the way a user would: setup commands, a script, and
   what each result would mean; or it says what the machine lacks (``blocked``). The
   playbook checks the script against the machine's and the repository's guards and
   submits it as ``lab.reproduce``: drafted for the maintainer who asked, or, for a
   review, decided by the repository's rule (a team PR may run at once, an outside
   contributor's waits for a maintainer). Then it waits.
2. Once the script is approved (or at once), the actuator runs it on the machine on
   the code before the PR and at its head.
3. The agent reads the runs. For a maintainer it drafts a comment. For a review it
   adds the result to the review draft while that waits for a decision, and drafts a
   comment once the review was posted; a run that did not happen (blocked, refused,
   not approved) is reported the same way. The runs themselves stay on the job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    ActionStatus,
    Autonomy,
    CommentUpsertPayload,
    LabReproducePayload,
    ProposalState,
    ReviewPostPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.labs import (
    forbidden,
    script_refusals,
    setup_lines,
    time_limit,
    unusable,
)
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, JobStatus
from ..domain.snapshot import PrSnapshot, PrState
from ..policy.engine import PolicyFacts, level_for
from .base import Playbook, PlaybookContext, PlaybookResult, Wait
from .prompts import (
    pr_block,
    reproduction_block,
    review_skill_block,
    schema_hint,
    untrusted,
)
from .review_revise import (
    answer_model,
    apply_revision,
    names,
    owns_format,
    revision_blocks,
    task_kind_of,
)

WAIT_SECONDS = 7 * 24 * 3600

PLAN = """You are planning a reproduction of a PR for {who} inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
The service runs your setup and script on test machine {lab}, in a fresh folder that holds only the repository's files, at the PR's head ("head") and, when comparing helps, on the code before the PR ("base").
- Reproduce it the way a user would. When the PR adds or changes a user-facing workflow (a model, an environment, an algorithm, a feature with docs) or claims a result, follow its documentation and description step by step: install, configure, fetch what the docs say to fetch, and run the documented commands, without changing the code to make them work. Have the script print each step it starts, so the first failure and anything the docs left out show in the output. Follow the repository's review skill where it says how to reproduce.
- When the PR only fixes a bug or changes behavior without a documented workflow, write the smallest check that settles it (a focused test or a short script) and run it on base and head.
- Set sides to ["head"] for a new workflow and to ["base", "head"] when comparing settles the claim (a fix, a speed-up).
- Say in expect what each side should show if the PR is right: the functional result and, when it claims one, the metric result, so the outcome can be read without you.
- The script must not need credentials, change anything outside its folder, or stop other people's work on the machine. Install what it needs in a throwaway environment inside its folder when the machine's environment lacks it.
- Set needs_gpu only when the run cannot happen without a GPU.
- When the machine cannot run it at all (it lacks the hardware, such as GPUs, an accelerator, or a robot, or assets or access the documented steps need), say so in blocked: what is missing and what a run would need; leave the script empty.
- You may read the code and run quick, read-only commands here. Do not commit, push, or post, and never ask whether to run it: {decides}
- {unclear}
{machine}"""

REPORT = """You are reporting a reproduction to {who} inside an automated maintainer service. The script below ran on test machine {lab} on the sides listed (the code before the PR, "base", and at its head, "head"); the end of each run's output follows.
- Decide whether the runs confirm what the PR claims: reproduced (head shows the documented or promised behavior, and base shows the problem when it ran), not_reproduced, or inconclusive (the script failed for reasons unrelated to the PR, timed out, or the output does not decide it).
- Write a short comment for the PR in {public_language}: what was run and how (the documented steps followed), on what kind of machine, what each side did, the first failure and any step the docs left out, the functional and the metric result, and the conclusion. Quote at most a few lines of output. Plain and specific; no scores, priorities, or internal names.
- Put anything only maintainers need (for example a broken environment on the machine) in notes."""

UPDATE = """You wrote the {what} of PR #{number} in {repo} given below, and it said a reproduction on a test machine would follow. That reproduction has ended; its record is below. Revise the {what} with it:
- Fill in where the skill asks how the PR was reproduced, what came out, and whether the documentation could be followed: the steps run and how, the functional and the metric result, the first failure and every step the docs left out; or, when no run happened, why and what a run would need.
- Change a finding or the recommendation only where the run settles it: it confirmed or disproved a claim, or the documented steps failed.
- Keep everything else word for word, including edits the reviewers made to the draft by hand.
- Return the whole revised {what} in `{field}`, and say in `reply`, in a sentence or two, what you changed.
- The {what} is public, in {public_language}: machine names, scores, and other internal details never go in it."""


@dataclass(frozen=True)
class AutoReproduction:
    """Whether a review's PR is reproduced on a test machine after the review."""

    lab: str | None = None
    """The machine the run goes to; ``None`` when no run starts."""
    at_once: bool = False
    """It runs without anyone approving its script: the repository's
    ``lab.reproduce`` rule says ``auto`` for this PR (for example, for its team)."""
    no_machine: bool = False
    """No run starts because the repository uses no machine that can take it."""

    def upcoming(self) -> str | None:
        """How the run starts, for the review's prompt; ``None`` when none starts."""
        if self.lab is None:
            return None
        return (
            "it runs at once"
            if self.at_once
            else "it waits for a maintainer to approve its script before it runs"
        )


async def auto_reproduction(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, trusted: bool
) -> AutoReproduction:
    """Whether reviewing ``pr`` starts a reproduction (design 5.10): the repository
    uses a machine that can take it, its ``lab.reproduce`` rule lets this PR run or be
    proposed (an outside contributor's script waits for a maintainer), and no
    reproduction is on record or underway at this head."""
    machine_for = next(
        (
            name
            for name, binding in profile.labs.items()
            if binding.use
            and unusable(pr.repo, name, ctx.service.machines.get(name), binding) is None
        ),
        None,
    )
    if machine_for is None:
        return AutoReproduction(no_machine=True)
    level = level_for(
        ActionKind.LAB_REPRODUCE,
        profile,
        PolicyFacts(author_is_team=trusted, changed_paths=pr.paths),
    )
    if level.rank < Autonomy.PROPOSE.rank:
        return AutoReproduction()
    for record in await ctx.store.list_actions(
        repo=pr.repo, number=pr.number, kinds=[ActionKind.LAB_REPRODUCE], limit=100
    ):
        if (
            record.action.head_sha == pr.head_sha
            and record.status != ActionStatus.SHADOWED
        ):
            return AutoReproduction()
    for job in await ctx.store.list_jobs(
        [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING], repo=pr.repo
    ):
        if job.playbook == "reproduce" and job.number == pr.number:
            return AutoReproduction()
    return AutoReproduction(lab=machine_for, at_once=level == Autonomy.AUTO)


class ReproPlan(BaseModel):
    summary: str = Field(description="What the script checks, in one or two sentences.")
    setup: str = Field(
        default="",
        description="Commands that prepare the code, such as installing it; may be empty.",
    )
    script: str = Field(
        default="",
        description="The bash script that follows the documented steps or checks the claim; empty when blocked.",
    )
    expect: str = Field(
        default="",
        description="What each side should show if the PR is right: the functional and, when claimed, the metric result.",
    )
    sides: list[Literal["base", "head"]] = Field(
        default_factory=lambda: ["base", "head"],
        description='["head"] for a new workflow; ["base", "head"] when comparing settles it.',
    )
    needs_gpu: bool = False
    timeout_minutes: int = Field(default=20, ge=1, le=360)
    blocked: str = Field(
        default="",
        description="When the machine cannot run it at all: what it lacks and what a run would need. Empty otherwise.",
    )


class ReproReport(BaseModel):
    conclusion: Literal["reproduced", "not_reproduced", "inconclusive"]
    comment: str = Field(description="The comment for the PR.")
    notes: str = Field(default="", description="For maintainers only.")


def _request(ctx: PlaybookContext, event: Event) -> Event:
    """The request that started the job: its first event (later ones only wake it)."""
    return Event.model_validate(ctx.job.events[0]) if ctx.job.events else event


def _for_review(request: Event) -> bool:
    return request.kind == EventKind.REVIEW_REPRODUCE


class ReproducePlaybook(Playbook):
    name = "reproduce"
    triggers = (EventKind.DASHBOARD_COMMAND, EventKind.REVIEW_REPRODUCE)
    pool = "agent"

    def accepts(self, event, profile) -> bool:  # type: ignore[no-untyped-def]
        if event.kind == EventKind.REVIEW_REPRODUCE:
            return (
                bool(event.payload.get("lab"))
                and bool(event.payload.get("review"))
                and event.number is not None
            )
        return (
            event.payload.get("command") == "reproduce"
            and bool(event.payload.get("by"))
            and bool(event.payload.get("lab"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        if event.kind == EventKind.REVIEW_REPRODUCE:
            return f"reproduce:{event.repo}#{event.number}:review:{event.payload.get('review')}"
        return f"reproduce:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        request = _request(ctx, event)
        assert request.repo and request.number
        by: str | None = request.payload.get("by") or None
        lab = str(request.payload["lab"])
        for_review = _for_review(request)
        ctx.requested_by = by
        ctx.agent = None if for_review else (request.payload.get("agent") or None)
        discriminator = f"req:{request.id}"
        mine = [
            r
            for r in await ctx.store.list_actions(
                repo=request.repo,
                number=request.number,
                kinds=[ActionKind.LAB_REPRODUCE, ActionKind.COMMENT_UPSERT],
            )
            if r.action.discriminator == discriminator
        ]
        planned = next(
            (r for r in mine if r.action.kind == ActionKind.LAB_REPRODUCE), None
        )
        reported = next(
            (r for r in mine if r.action.kind == ActionKind.COMMENT_UPSERT), None
        )
        if reported is not None:
            return PlaybookResult(result={"reported": reported.action.id})
        if planned is not None:
            return await self._after_run(ctx, request, event, planned, by, lab)

        pr = await ctx.forge.get_pr(request.repo, request.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        machine = ctx.service.machines.get(lab)
        binding = profile.labs.get(lab)
        why = unusable(pr.repo, lab, machine, binding)
        if why or machine is None:
            return await self._not_run(
                ctx, request, pr, why or f"{lab} is not set up", reason=why
            )
        before = setup_lines(machine, binding)
        limit = time_limit(machine, binding)
        ctx.trusted_input = await ctx.author_is_team(pr)
        at_once = (
            for_review
            and level_for(
                ActionKind.LAB_REPRODUCE,
                profile,
                PolicyFacts(author_is_team=ctx.trusted_input, changed_paths=pr.paths),
            )
            == Autonomy.AUTO
        )
        who = f"{by}, a maintainer" if by else "the repository's maintainers"
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            about = "\n".join(
                line
                for line in (
                    f"About {lab}: {machine.description}"
                    if machine.description
                    else "",
                    "Before your setup, every run does: " + " && ".join(before)
                    if before
                    else "",
                    f"A run may take at most {limit // 60} minutes.",
                )
                if line
            )
            note = str(request.payload.get("note") or "").strip()
            prompt = "\n\n".join(
                part
                for part in [
                    PLAN.format(
                        who=who,
                        lab=lab,
                        machine=about,
                        decides=(
                            "it runs on its own, so it must be safe to run unattended."
                            if at_once
                            else f"{by} reads the script and decides whether it runs."
                            if by and not for_review
                            else "a maintainer reads the script and decides whether it runs."
                        ),
                        unclear=(
                            f"When the claim is unclear or several checks are equally plausible, ask {by} with your tool for asking the user."
                            if by
                            else "When the claim is unclear, follow the documentation and the PR description as written and say in summary what you assumed."
                        ),
                    ),
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    f"What {by} wants checked: "
                    + untrusted("maintainer.note", note, 2000)
                    if note
                    else "",
                    pr_block(pr, diff),
                    schema_hint(ReproPlan.model_json_schema()),
                ]
                if part
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.REPRODUCE,
                    prompt=prompt,
                    workdir=workdir,
                    mode="read_only",
                    output_schema=ReproPlan.model_json_schema(),
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    timeout_seconds=1800,
                ),
                ReproPlan,
            )
        finally:
            await ctx.workspace.release(workdir)
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"reproduction agent failed: {answer.error}")
        plan = ReproPlan.model_validate(answer.structured)
        if plan.blocked.strip() or not plan.script.strip():
            return await self._not_run(
                ctx,
                request,
                pr,
                f"the machine cannot run it: {plan.blocked.strip() or 'no script was written'}",
                reason="blocked",
                plan=plan,
            )
        refused = script_refusals(
            "\n".join([*before, plan.setup, plan.script]), forbidden(machine, binding)
        )
        if refused:
            return await self._not_run(
                ctx,
                request,
                pr,
                "the documented steps need commands the test machine does not allow: "
                + "; ".join(refused),
                reason="the script would not be allowed on the machine",
                plan=plan,
                refused=refused,
            )
        action = make_action(
            ActionKind.LAB_REPRODUCE,
            pr.repo,
            LabReproducePayload(
                lab=lab,
                summary=plan.summary,
                setup=plan.setup,
                script=plan.script,
                expect=plan.expect,
                needs_gpu=plan.needs_gpu,
                timeout_seconds=min(plan.timeout_minutes * 60, limit),
                sides=plan.sides or ["base", "head"],
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=discriminator,
            rationale=(
                f"a reproduction on {lab} for the review at {pr.head_sha[:7]}: {plan.summary}"
                if for_review
                else f"a reproduction on {lab}, asked by {by}: {plan.summary}"
            ),
            hints=ActionHints(author_is_team=ctx.trusted_input, changed_paths=pr.paths),
            draft_for=None if for_review else by,
        )
        return PlaybookResult(
            actions=[action],
            wait=Wait(
                kinds=[
                    EventKind.ACTION_EXECUTED,
                    EventKind.ACTION_FAILED,
                    EventKind.PROPOSAL_DECIDED,
                ],
                seconds=WAIT_SECONDS,
                reason=_waiting(by, for_review),
                action_ids=[action.id],
            ),
            result={"planned": plan.summary, "action": action.id},
        )

    async def _after_run(
        self,
        ctx: PlaybookContext,
        request: Event,
        event: Event,
        planned: ActionRecord,
        by: str | None,
        lab: str,
    ) -> PlaybookResult:
        record = await ctx.store.get_action(planned.action.id) or planned
        payload = LabReproducePayload.model_validate(record.action.payload)
        pr = await ctx.forge.get_pr(request.repo or "", request.number or 0)
        if record.status in (
            ActionStatus.NEW,
            ActionStatus.PROPOSED,
            ActionStatus.QUEUED,
            ActionStatus.INTENT,
        ):
            if event.kind == EventKind.JOB_TIMEOUT:
                return await self._not_run(
                    ctx,
                    request,
                    pr,
                    "nobody approved the reproduction's script in time",
                    reason="nobody approved the script in time",
                )
            return PlaybookResult(
                wait=Wait(
                    kinds=[
                        EventKind.ACTION_EXECUTED,
                        EventKind.ACTION_FAILED,
                        EventKind.PROPOSAL_DECIDED,
                    ],
                    seconds=WAIT_SECONDS,
                    reason=_waiting(by, _for_review(request)),
                    action_ids=[record.action.id],
                ),
                result={"action": record.action.id},
            )
        if record.status == ActionStatus.DENIED:
            return await self._not_run(
                ctx,
                request,
                pr,
                "a maintainer did not approve the reproduction's script",
                reason="the script was not approved",
            )
        if record.status == ActionStatus.SHADOWED:
            return await self._not_run(
                ctx,
                request,
                pr,
                "the repository's settings record reproductions without running them",
                reason="the repository records reproductions without running them",
            )
        if record.status != ActionStatus.EXECUTED:
            return await self._not_run(
                ctx,
                request,
                pr,
                f"the run did not happen ({record.status}): {record.error or 'no detail'}",
                reason=f"the reproduction did not run ({record.status})",
                error=record.error,
            )
        outcome = record.external_ref
        if outcome.get("refused") or outcome.get("deferred"):
            return await self._not_run(
                ctx,
                request,
                pr,
                f"the machine did not run it: {outcome.get('reason') or outcome.get('refused')}",
                reason="the machine did not run it",
                refused=outcome.get("refused"),
                machine_reason=outcome.get("reason"),
            )
        profile = ctx.require_profile()
        runs = outcome.get("runs") or {}
        who = f"{by}, a maintainer" if by else "the repository's maintainers"
        prompt = "\n\n".join(
            [
                REPORT.format(
                    who=who, lab=lab, public_language=profile.languages.public
                ),
                "The reproduction: "
                + untrusted(
                    "reproduction.plan",
                    json.dumps(
                        {
                            "summary": payload.summary,
                            "setup": payload.setup,
                            "script": payload.script,
                            "expect": payload.expect,
                            "sides": payload.sides,
                        },
                        ensure_ascii=False,
                        indent=1,
                    ),
                    20_000,
                ),
                "The runs: "
                + untrusted(
                    "reproduction.runs",
                    json.dumps(runs, ensure_ascii=False, indent=1),
                    30_000,
                ),
                schema_hint(ReproReport.model_json_schema()),
            ]
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.REPRODUCE,
                prompt=prompt,
                workdir=ctx.scratch(),
                mode="read_only",
                output_schema=ReproReport.model_json_schema(),
                repo=request.repo,
                timeout_seconds=600,
            ),
            ReproReport,
        )
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"reproduction report failed: {answer.error}")
        report = ReproReport.model_validate(answer.structured)
        summary = {
            "conclusion": report.conclusion,
            "notes": report.notes,
            "runs": {
                side: {k: v for k, v in run.items() if k != "tail"}
                for side, run in runs.items()
            },
        }
        if _for_review(request) and pr is not None:
            machine = ctx.service.machines.get(lab)
            record_text = untrusted(
                "reproduction",
                json.dumps(
                    {
                        "machine": machine.description if machine else "",
                        "commit": record.action.head_sha,
                        "plan": {
                            "summary": payload.summary,
                            "setup": payload.setup,
                            "script": payload.script,
                            "expect": payload.expect,
                            "sides": payload.sides,
                        },
                        "runs": runs,
                        "conclusion": report.conclusion,
                        "report": report.comment,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                40_000,
            )
            revised = await self._revise(ctx, request, pr, record_text)
            if revised is not None:
                return PlaybookResult(result={**summary, "revised": revised})
        return PlaybookResult(
            actions=[
                self._comment(
                    request, record.action.head_sha, report.comment, by, answer.runtime
                )
            ],
            result=summary,
        )

    async def _not_run(
        self,
        ctx: PlaybookContext,
        request: Event,
        pr: PrSnapshot | None,
        why: str,
        *,
        reason: str | None,
        plan: ReproPlan | None = None,
        **extra: object,
    ) -> PlaybookResult:
        """End without a run. For a review, its draft says why and what a run would
        need; for a maintainer, the job's result does."""
        result: dict[str, object] = {"skipped": reason or why, **extra}
        if plan is not None:
            result["script"] = plan.script
            if plan.blocked:
                result["blocked"] = plan.blocked
        if _for_review(request) and pr is not None:
            record_text = untrusted(
                "reproduction",
                json.dumps(
                    {
                        "ran": False,
                        "why": why,
                        "what_was_planned": plan.summary if plan else "",
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                10_000,
            )
            revised = await self._revise(ctx, request, pr, record_text)
            if revised is not None:
                result["revised"] = revised
        return PlaybookResult(result=result)

    async def _revise(
        self, ctx: PlaybookContext, request: Event, pr: PrSnapshot, record_text: str
    ) -> str | None:
        """Add the reproduction's record to the review draft while it waits for a
        decision; the id of the draft, or ``None`` when it was decided (or is gone)
        or the agent could not revise it."""
        review_id = str(request.payload.get("review") or "")
        record = await ctx.store.get_action(review_id) if review_id else None
        proposal = await ctx.store.proposal_for_action(review_id) if record else None
        if (
            record is None
            or proposal is None
            or proposal.state != ProposalState.PENDING
            or ctx.drafts is None
        ):
            return None
        profile = ctx.require_profile()
        payload = ReviewPostPayload.model_validate(record.action.payload)
        kind = task_kind_of(payload)
        head = record.action.head_sha or pr.head_sha
        what, field = names(kind)
        model = answer_model(kind)
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, head, info.default_branch
        )
        try:
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", head
            )
            own_format = owns_format(kind, profile, workdir)
            prompt = "\n\n".join(
                part
                for part in [
                    UPDATE.format(
                        what=what,
                        number=pr.number,
                        repo=pr.repo,
                        field=field,
                        public_language=profile.languages.public,
                    ),
                    *revision_blocks(
                        kind=kind,
                        profile=profile,
                        skills_dir=ctx.service.skills_dir,
                        workdir=workdir,
                        reproduction=reproduction_block(record_text),
                        lessons=await ctx.knowledge.review_lessons(pr.repo, profile),
                        pr=pr,
                        head=head,
                        default_branch=info.default_branch,
                        diff=diff,
                        payload=payload,
                    ),
                    schema_hint(model.model_json_schema()),
                ]
                if part
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=kind,
                    prompt=prompt,
                    workdir=workdir,
                    mode="read_only",
                    output_schema=model.model_json_schema(),
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                ),
                model,
            )
        finally:
            await ctx.workspace.release(workdir)
        if not answer.ok or answer.structured is None:
            return None
        new_payload = apply_revision(
            model.model_validate(answer.structured),
            payload,
            profile,
            pr,
            head,
            own_format,
        )
        if new_payload is None:
            return None
        try:
            await ctx.drafts.save_edits(
                proposal.id, ctx.service.login, new_payload, person=False
            )
        except ValueError:
            return None  # decided while the agent worked
        return review_id

    @staticmethod
    def _comment(
        request: Event,
        head_sha: str | None,
        body: str,
        by: str | None,
        runtime: str | None,
    ) -> Action:
        return make_action(
            ActionKind.COMMENT_UPSERT,
            request.repo or "",
            CommentUpsertPayload(purpose="reproduction", body=body, runtime=runtime),
            number=request.number,
            head_sha=head_sha,
            discriminator=f"req:{request.id}",
            rationale="the result of a reproduction"
            + (f", asked by {by}" if by else ", after its review was posted"),
            draft_for=by,
        )


def _waiting(by: str | None, for_review: bool) -> str:
    if for_review:
        return "waiting for the reproduction to run, or for a maintainer to approve its script"
    return f"waiting for {by} to approve the script"
