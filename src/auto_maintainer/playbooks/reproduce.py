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

1. An agent reads the PR in a checkout of its head and plans a reproduction that
   follows the PR's documentation the way a user would: the steps in plain words,
   which sides to run, and what each result would mean; or it says what the machine
   lacks (``blocked``). The playbook submits the plan as ``lab.reproduce``: drafted
   for the maintainer who asked, or, for a review, decided by the repository's rule
   (a team PR may run at once, an outside contributor's waits for a maintainer).
2. Once the plan is approved (or at once), the actuator opens a session on the
   machine with the code before the PR and at its head, and an agent does the
   reproduction there itself, one command at a time through ``lab``: each command is
   checked against the machine's rules, and its output comes back to the agent and
   to the job's live view as it runs. When a step fails for a reason in the machine's
   environment, the agent finds the cause and fixes it, or asks the maintainer who
   asked what to do. The playbook closes the session when the agent is done.
3. The agent's report goes into the PR's review: the review draft while it waits for
   a decision, an edit of the review once it was posted, and a comment only for a PR
   with no review. A run that did not happen (blocked, not approved, no free GPU) is
   reported the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    ActionStatus,
    Autonomy,
    CommentUpsertPayload,
    LabReproducePayload,
    Proposal,
    ProposalState,
    ReviewPostPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.labs import (
    setup_lines,
    time_limit,
    unusable,
)
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, JobStatus
from ..domain.snapshot import PrSnapshot, PrState
from ..policy.engine import PolicyFacts, level_for
from .base import (
    Playbook,
    PlaybookContext,
    PlaybookResult,
    Wait,
    may_succeed_later,
)
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
    posted_update_action,
    revision_blocks,
    task_kind_of,
)

WAIT_SECONDS = 7 * 24 * 3600

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to write its report."""

PLAN = """You are planning a reproduction of a PR for {who} inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
Once the plan is approved, an agent does the reproduction itself on test machine {lab}: the service copies the code before the PR ("base") and at its head ("head") there, and the agent runs commands one at a time, reads their output, and decides the next step. Plan it now:
- Reproduce it the way a user would. When the PR adds or changes a user-facing workflow (a model, an environment, an algorithm, a feature with docs) or claims a result, follow its documentation and description step by step: install, configure, fetch what the docs say to fetch, and run the documented commands, without changing the code to make them work. Follow the repository's review skill where it says how to reproduce.
- When the PR only fixes a bug or changes behavior without a documented workflow, plan the smallest check that settles it (a focused test or a short script), on base and head.
- List the steps in `steps`, in plain words, as the person approving reads them. Set sides to ["head"] for a new workflow and to ["base", "head"] when comparing settles the claim. Say in expect what each side should show if the PR is right: the functional result and, when it claims one, the metric result. Set needs_gpu only when the run cannot happen without a GPU, and timeout_minutes for the whole session.
- When the machine cannot run it at all (it lacks the hardware, such as GPUs, an accelerator, or a robot, or assets or access the documented steps need), say so in blocked: what is missing and what a run would need.
- Read the code and its git history here, and run nothing on this machine. Do not commit, push, or post, and never ask whether to run it: {decides}
- {unclear}
{machine}"""

SESSION = """You are reproducing PR #{number} in {repo} for {who} on test machine {lab}, inside an automated maintainer service. The plan below was approved; carry it out yourself.
- Run commands on the machine with `lab -- 'COMMAND'` (one quoted string) for the PR's head{base_note}. Each runs in a fresh shell in that side's folder on the machine, after the machine's own setup: chain what needs the same shell with `&&`, and keep virtual environments, caches, and downloads inside the folder (activate an environment in each command that needs it). `lab --timeout MINUTES -- 'COMMAND'` gives a long step more time, and `lab --info` says how long the session has left. A command's output comes back as it runs, and `lab` exits with its exit code.
- The working directory here is a checkout of the PR's head, to read the code and docs; the PR's code runs only on the machine, through `lab`.
- Go step by step: run a step, read its output, decide the next. Follow the documentation the way a user would, and note every step it leaves out.
- When a step fails for a reason in the machine's environment or the setup rather than in the PR (a missing package, a wrong path or command, a mirror, proxy, or download problem), find the cause in the output, or run what finds it (versions, paths, the failing command alone), fix it, and say what you changed. {ask}
- A failure the PR itself causes is the result: report it; do not work around it.
- Never use sudo, change anything outside the session's folders, or stop other people's work on the machine; the machine refuses commands that would.
- Finish within the session's time with the JSON your task asks for: the conclusion (reproduced: head shows the documented or promised behavior, and base shows the problem when it ran; not_reproduced; or inconclusive), a short comment for the PR in {public_language} (what was run and how, on what kind of machine, what each side did, the first failure and any step the docs left out, the functional and the metric result, and the conclusion; quote at most a few lines of output; no scores, priorities, or internal names), and notes for maintainers only (for example what the machine lacks)."""

ASK = (
    "When you cannot fix it, because the machine needs a change only its "
    "administrators can make, or you cannot tell whether the PR or the machine causes "
    "it, ask {by} with your tool for asking the user what to do, with what you found "
    "and the choices you see, and follow the answer."
)
NO_ASK = (
    "When you cannot fix it, because the machine needs a change only its "
    "administrators can make, say so in notes, with what the machine needs and why, "
    "and finish."
)

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
    """It runs without anyone approving its plan: the repository's
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
            else "it waits for a maintainer to approve its plan before it runs"
        )


async def auto_reproduction(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, trusted: bool
) -> AutoReproduction:
    """Whether reviewing ``pr`` starts a reproduction (design 5.10): the repository
    uses a machine that can take it, its ``lab.reproduce`` rule lets this PR run or be
    proposed (an outside contributor's plan waits for a maintainer), and no
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
    summary: str = Field(
        description="What the reproduction checks, in one or two sentences."
    )
    steps: list[str] = Field(
        default_factory=list,
        description="The plan, step by step in plain words, for the person approving it.",
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
    timeout_minutes: int = Field(default=30, ge=1, le=360)
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
                    f"The environment and rules of {lab}, from its maintainers; follow "
                    f"them:\n{machine.description.strip()}"
                    if machine.description.strip()
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
                            "it runs on its own, so plan only what is safe to run unattended."
                            if at_once
                            else f"{by} reads the plan and decides whether it runs."
                            if by and not for_review
                            else "a maintainer reads the plan and decides whether it runs."
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
        if plan.blocked.strip() or not (plan.steps or plan.summary.strip()):
            return await self._not_run(
                ctx,
                request,
                pr,
                f"the machine cannot run it: {plan.blocked.strip() or 'no plan was written'}",
                reason="blocked",
                plan=plan,
            )
        action = _plan_action(
            pr,
            plan,
            lab=lab,
            limit=limit,
            by=by,
            for_review=for_review,
            discriminator=discriminator,
            runtime=answer.runtime,
            team=ctx.trusted_input,
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
                    "nobody approved the reproduction's plan in time",
                    reason="nobody approved the plan in time",
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
                "a maintainer did not approve the reproduction's plan",
                reason="the plan was not approved",
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
        if outcome.get("error"):
            return await self._not_run(
                ctx,
                request,
                pr,
                f"the session did not open: {outcome['error']}",
                reason="the session did not open",
            )
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
        session_id = str(outcome.get("session") or "")
        token = (
            ctx.lab_sessions.token(session_id)
            if session_id and ctx.lab_sessions is not None
            else None
        )
        if token is None or pr is None or ctx.lab_sessions is None:
            if session_id and ctx.lab_sessions is not None:
                await ctx.lab_sessions.close(session_id)
            return await self._not_run(
                ctx,
                request,
                pr,
                "the session on the machine ended before an agent could work in it "
                "(the service restarted or its time ran out); ask for it again",
                reason="the session ended before the agent worked",
            )
        report, runtime, commands = await self._work(
            ctx, request, pr, payload, record, by, lab, session_id, token
        )
        summary = {
            "conclusion": report.conclusion,
            "notes": report.notes,
            "commands": [{k: v for k, v in c.items() if k != "tail"} for c in commands],
        }
        runs = {"commands": commands}
        if pr is not None:
            machine = ctx.service.machines.get(lab)
            record_text = untrusted(
                "reproduction",
                json.dumps(
                    {
                        "machine": machine.description if machine else "",
                        "commit": record.action.head_sha,
                        "plan": {
                            "summary": payload.summary,
                            "steps": payload.steps,
                            "expect": payload.expect,
                            "sides": payload.sides,
                        },
                        "commands": runs["commands"],
                        "conclusion": report.conclusion,
                        "report": report.comment,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                40_000,
            )
            target = await self._review_of(ctx, request, pr)
            if target is not None and target[1] is not None:
                revised = await self._revise(ctx, target[0], target[1], pr, record_text)
                if revised is not None:
                    return PlaybookResult(result={**summary, "revised": revised})
            elif target is not None:
                update = await self._update_posted(
                    ctx, target[0], pr, record_text, by, request
                )
                if update is not None:
                    return PlaybookResult(
                        actions=[update],
                        result={**summary, "updates": target[0].action.id},
                    )
        return PlaybookResult(
            actions=[
                self._comment(
                    request, record.action.head_sha, report.comment, by, runtime
                )
            ],
            result=summary,
        )

    async def _work(
        self,
        ctx: PlaybookContext,
        request: Event,
        pr: PrSnapshot,
        payload: LabReproducePayload,
        record: ActionRecord,
        by: str | None,
        lab: str,
        session_id: str,
        token: str,
    ) -> tuple[ReproReport, str | None, list[dict[str, Any]]]:
        """Have an agent do the reproduction in its session on the machine; close the
        session when it is done, and return its report, its runtime, and the commands
        that ran. A failure that may pass later leaves the session open for the
        job's next try, until its time limit."""
        assert ctx.lab_sessions is not None
        profile = ctx.require_profile()
        machine = ctx.service.machines.get(lab)
        who = f"{by}, a maintainer" if by else "the repository's maintainers"
        prompt = "\n\n".join(
            part
            for part in [
                SESSION.format(
                    number=pr.number,
                    repo=pr.repo,
                    who=who,
                    lab=lab,
                    base_note=(
                        ", and `lab --side base -- 'COMMAND'` for the code before the PR"
                        if "base" in payload.sides
                        else ""
                    ),
                    ask=ASK.format(by=by) if by else NO_ASK,
                    public_language=profile.languages.public,
                ),
                f"The environment and rules of {lab}, from its maintainers; follow "
                f"them:\n{machine.description.strip()}"
                if machine and machine.description.strip()
                else "",
                "The approved plan:\n"
                + json.dumps(
                    {
                        "summary": payload.summary,
                        "steps": payload.steps,
                        "expect": payload.expect,
                        "sides": payload.sides,
                        "minutes": payload.timeout_seconds // 60,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                f"What {by} wants checked: "
                + untrusted("maintainer.note", str(request.payload.get("note")), 2000)
                if request.payload.get("note")
                else "",
                pr_block(pr),
                schema_hint(ReproReport.model_json_schema()),
            ]
            if part
        )
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo,
            pr.number,
            record.action.head_sha or pr.head_sha,
            info.default_branch,
        )
        answer = None
        try:
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.REPRODUCE,
                    prompt=prompt,
                    workdir=workdir,
                    mode="workspace_write",
                    output_schema=ReproReport.model_json_schema(),
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    timeout_seconds=payload.timeout_seconds + SESSION_GRACE_SECONDS,
                    lab_session=token,
                    env={"AM_LAB_SOCKET": str(ctx.service.lab_socket or "")},
                ),
                ReproReport,
            )
        finally:
            await ctx.workspace.release(workdir)
            keep = answer is not None and not answer.ok and may_succeed_later(answer)
            closed = None if keep else await ctx.lab_sessions.close(session_id)
        if answer is None or not answer.ok or answer.structured is None:
            raise RuntimeError(
                f"the reproduction agent failed: {answer.error if answer else 'no answer'}"
            )
        commands = list((closed or {}).get("commands") or [])
        return ReproReport.model_validate(answer.structured), answer.runtime, commands

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
            result["steps"] = plan.steps
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
            target = await self._review_of(ctx, request, pr)
            if target is not None and target[1] is not None:
                revised = await self._revise(ctx, target[0], target[1], pr, record_text)
                if revised is not None:
                    result["revised"] = revised
        return PlaybookResult(result=result)

    async def _review_of(
        self, ctx: PlaybookContext, request: Event, pr: PrSnapshot
    ) -> tuple[ActionRecord, Proposal | None] | None:
        """The review a reproduction's result belongs in, with its proposal while it
        waits for a decision: the review that asked for the run, else the PR's
        newest review that waits or was posted. ``None`` when the PR has none."""
        wanted = str(request.payload.get("review") or "")
        records = (
            [r for r in [await ctx.store.get_action(wanted)] if r is not None]
            if wanted
            else sorted(
                await ctx.store.list_actions(
                    repo=pr.repo, number=pr.number, kinds=[ActionKind.REVIEW_POST]
                ),
                key=lambda r: r.action.created_at,
                reverse=True,
            )
        )
        for record in records:
            proposal = await ctx.store.proposal_for_action(record.action.id)
            if proposal is not None and proposal.state == ProposalState.PENDING:
                return record, proposal
            if record.status == ActionStatus.EXECUTED and record.external_ref.get(
                "review_id"
            ):
                return record, None
        return None

    async def _update_posted(
        self,
        ctx: PlaybookContext,
        record: ActionRecord,
        pr: PrSnapshot,
        record_text: str,
        by: str | None,
        request: Event,
    ) -> Action | None:
        """An edit of the posted review that adds the reproduction's record, for
        whoever posted it to approve; ``None`` when the agent could not revise it."""
        new_payload = await self._revised_payload(ctx, record, pr, record_text)
        if new_payload is None:
            return None
        return posted_update_action(
            record,
            new_payload,
            discriminator=f"req:{request.id}:review",
            rationale="add the result of a reproduction to the posted review"
            + (f", asked by {by}" if by else ""),
            draft_for=by,
        )

    async def _revise(
        self,
        ctx: PlaybookContext,
        record: ActionRecord,
        proposal: Proposal,
        pr: PrSnapshot,
        record_text: str,
    ) -> str | None:
        """Add the reproduction's record to the review draft while it waits for a
        decision; the id of the draft, or ``None`` when it was decided while the
        agent worked or the agent could not revise it."""
        if ctx.drafts is None:
            return None
        new_payload = await self._revised_payload(ctx, record, pr, record_text)
        if new_payload is None:
            return None
        try:
            await ctx.drafts.save_edits(
                proposal.id, ctx.service.login, new_payload, person=False
            )
        except ValueError:
            return None  # decided while the agent worked
        return record.action.id

    async def _revised_payload(
        self,
        ctx: PlaybookContext,
        record: ActionRecord,
        pr: PrSnapshot,
        record_text: str,
    ) -> dict[str, Any] | None:
        """The review ``record`` drafted or posted, revised by an agent with the
        reproduction's record, rendered as the playbook renders it."""
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
        return new_payload

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


def _plan_action(
    pr: PrSnapshot,
    plan: ReproPlan,
    *,
    lab: str,
    limit: int,
    by: str | None,
    for_review: bool,
    discriminator: str,
    runtime: str | None,
    team: bool,
) -> Action:
    """The ``lab.reproduce`` action that runs ``plan``: drafted for the maintainer
    who asked, or decided by the repository's rule for a review's reproduction."""
    return make_action(
        ActionKind.LAB_REPRODUCE,
        pr.repo,
        LabReproducePayload(
            lab=lab,
            summary=plan.summary,
            steps=plan.steps,
            expect=plan.expect,
            needs_gpu=plan.needs_gpu,
            timeout_seconds=min(plan.timeout_minutes * 60, limit),
            sides=plan.sides or ["base", "head"],
            runtime=runtime,
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
        hints=ActionHints(author_is_team=team, changed_paths=pr.paths),
        draft_for=None if for_review else by,
    )


def _waiting(by: str | None, for_review: bool) -> str:
    if for_review:
        return "waiting for the reproduction to run, or for a maintainer to approve its plan"
    return f"waiting for {by} to approve the plan"
