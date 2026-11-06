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

A maintainer asks for it from the PR (``dashboard.command`` with ``command:
reproduce``), naming a machine. In three steps, each starting from fresh reads:

1. An agent reads the PR in a checkout of its head and writes a reproduction: setup
   commands and a script, and what each result would mean. It may ask the maintainer.
   The machine must be one the repository uses (its profile's ``labs``), whose setup
   and limits apply. The playbook checks the script against the machine's and the
   repository's guards and proposes it as a
   ``lab.reproduce`` action drafted for the maintainer, then waits.
2. Once the maintainer approves the script, the actuator runs it on the machine on
   the code before the PR and at its head.
3. The agent reads both runs and drafts a comment for the maintainer, who posts or
   drops it. The runs themselves stay internal, on the job.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionKind,
    ActionStatus,
    CommentUpsertPayload,
    LabReproducePayload,
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
from ..domain.records import ActionRecord
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, Wait
from .prompts import pr_block, review_skill_block, schema_hint, untrusted

WAIT_SECONDS = 7 * 24 * 3600

PLAN = """You are planning a reproduction for {by}, a maintainer, inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
Write a bash script that shows whether what the PR claims holds: the bug it fixes, the behavior it adds, or the speed-up it promises. The service runs your setup and script twice on test machine {lab}: once in a copy of the code before this PR ("base") and once at its head ("head"), each in a fresh folder that holds only the repository's files.
- Prefer the smallest check that settles the claim: a focused test, a short script, a command from the PR description. Do not run the whole test suite unless the claim needs it.
- The script must not need credentials, change anything outside its folder, or stop other people's work on the machine. Install what it needs in a throwaway environment inside its folder when the machine's environment lacks it.
- Say what result each side should give if the PR is right, so the outcome can be read without you.
- Set needs_gpu only when the claim cannot be checked without a GPU.
- You may read the code and run quick, read-only commands here. Do not commit, push, or post, and never ask whether to: {by} reads the script and decides whether it runs.
- When the claim is unclear or several checks are equally plausible, ask {by} with your tool for asking the user.
{machine}"""

REPORT = """You are reporting a reproduction to {by}, a maintainer, inside an automated maintainer service. The script below ran on test machine {lab} on the code before the PR ("base") and at its head ("head"); the end of each run's output follows.
- Decide whether the runs confirm what the PR claims: reproduced (base shows the problem, head does not, or head shows the promised behavior), not_reproduced, or inconclusive (the script failed for unrelated reasons, timed out, or the output does not decide it).
- Write a short comment for the PR in {public_language}: what was checked, on what kind of machine, what each side did, and the conclusion. Quote at most a few lines of output. Plain and specific; no scores, priorities, or internal names.
- Put anything only maintainers need (for example a broken environment on the machine) in notes."""


class ReproPlan(BaseModel):
    summary: str = Field(description="What the script checks, in one or two sentences.")
    setup: str = Field(
        default="",
        description="Commands that prepare the code, such as installing it; may be empty.",
    )
    script: str = Field(description="The bash script that checks the claim.")
    expect: str = Field(
        description="What each side (base, head) should show if the PR is right."
    )
    needs_gpu: bool = False
    timeout_minutes: int = Field(default=20, ge=1, le=360)


class ReproReport(BaseModel):
    conclusion: Literal["reproduced", "not_reproduced", "inconclusive"]
    comment: str = Field(description="The comment for the PR.")
    notes: str = Field(default="", description="For maintainers only.")


def _request(ctx: PlaybookContext, event: Event) -> Event:
    """The maintainer's request: the job's first event (later ones only wake it)."""
    return Event.model_validate(ctx.job.events[0]) if ctx.job.events else event


class ReproducePlaybook(Playbook):
    name = "reproduce"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event, profile) -> bool:  # type: ignore[no-untyped-def]
        return (
            event.payload.get("command") == "reproduce"
            and bool(event.payload.get("by"))
            and bool(event.payload.get("lab"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        return f"reproduce:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        request = _request(ctx, event)
        assert request.repo and request.number
        by = str(request.payload["by"])
        lab = str(request.payload["lab"])
        ctx.requested_by = by
        ctx.agent = request.payload.get("agent") or None
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
            return PlaybookResult(result={"skipped": why})
        before = setup_lines(machine, binding)
        limit = time_limit(machine, binding)
        ctx.trusted_input = await ctx.author_is_team(pr)
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
                    PLAN.format(by=by, lab=lab, machine=about),
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
        refused = script_refusals(
            "\n".join([*before, plan.setup, plan.script]), forbidden(machine, binding)
        )
        if refused:
            return PlaybookResult(
                result={
                    "skipped": "the script would not be allowed on the machine",
                    "refused": refused,
                    "script": plan.script,
                }
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
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=discriminator,
            rationale=f"a reproduction on {lab}, asked by {by}: {plan.summary}",
            draft_for=by,
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
                reason=f"waiting for {by} to approve the script",
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
        by: str,
        lab: str,
    ) -> PlaybookResult:
        record = await ctx.store.get_action(planned.action.id) or planned
        if record.status in (
            ActionStatus.NEW,
            ActionStatus.PROPOSED,
            ActionStatus.QUEUED,
            ActionStatus.INTENT,
        ):
            if event.kind == EventKind.JOB_TIMEOUT:
                return PlaybookResult(
                    result={"skipped": "nobody approved the script in time"}
                )
            return PlaybookResult(
                wait=Wait(
                    kinds=[
                        EventKind.ACTION_EXECUTED,
                        EventKind.ACTION_FAILED,
                        EventKind.PROPOSAL_DECIDED,
                    ],
                    seconds=WAIT_SECONDS,
                    reason=f"waiting for {by} to approve the script",
                    action_ids=[record.action.id],
                ),
                result={"action": record.action.id},
            )
        if record.status == ActionStatus.DENIED:
            return PlaybookResult(result={"skipped": "the script was not approved"})
        if record.status != ActionStatus.EXECUTED:
            return PlaybookResult(
                result={
                    "skipped": f"the reproduction did not run ({record.status})",
                    "error": record.error,
                }
            )
        outcome = record.external_ref
        if outcome.get("refused") or outcome.get("deferred"):
            return PlaybookResult(
                result={
                    "skipped": "the machine did not run it",
                    "refused": outcome.get("refused"),
                    "reason": outcome.get("reason"),
                }
            )
        profile = ctx.require_profile()
        payload = LabReproducePayload.model_validate(record.action.payload)
        runs = outcome.get("runs") or {}
        prompt = "\n\n".join(
            [
                REPORT.format(by=by, lab=lab, public_language=profile.languages.public),
                "The reproduction: "
                + untrusted(
                    "reproduction.plan",
                    json.dumps(
                        {
                            "summary": payload.summary,
                            "setup": payload.setup,
                            "script": payload.script,
                            "expect": payload.expect,
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
        scratch = ctx.scratch()
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.REPRODUCE,
                prompt=prompt,
                workdir=scratch,
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
        comment = make_action(
            ActionKind.COMMENT_UPSERT,
            request.repo,
            CommentUpsertPayload(
                purpose="reproduction", body=report.comment, runtime=answer.runtime
            ),
            number=request.number,
            head_sha=record.action.head_sha,
            discriminator=f"req:{request.id}",
            rationale=f"reproduction on {lab}: {report.conclusion}",
            draft_for=by,
        )
        return PlaybookResult(
            actions=[comment],
            result={
                "conclusion": report.conclusion,
                "notes": report.notes,
                "runs": {
                    side: {k: v for k, v in run.items() if k != "tail"}
                    for side, run in runs.items()
                },
            },
        )
