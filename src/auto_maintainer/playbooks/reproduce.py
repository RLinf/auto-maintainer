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

A reproduction is the review skill's reproduction agent: a fresh agent, apart from
the one that reviewed the PR, that follows the brief the reviewer wrote (or what a
person asked to have checked) as the skill says its reproduction agent works. It
starts in one of two ways: a review whose skill calls for a reproduction returns the
brief (``ReviewResult.reproduction``), or a person asks for one from the PR, naming a
machine, or in the conversation about a review. Nobody approves it first: a review's
runs under the repository's ``lab.review`` rule, like the review's own session, and a
person's request is its approval (``pipeline.RUN_ON_REQUEST``). In order, each step
from fresh reads:

1. The playbook asks for a session on the machine (``lab.reproduce``, or
   ``lab.review`` for a review's), which the actuator opens with the code before the
   PR and at its head, with an idle GPU when there is one.
2. The agent reads its skill and the brief, and works there through ``lab``: each
   command is checked against the machine's rules, its output comes back to the agent
   and to the job's live view as it runs, and the agent takes more GPUs, waiting for
   them to be idle, when the run needs them (``lab --gpus``). The playbook closes
   the session when the agent is done.
3. The agent's record goes into the PR's review: the review draft while it waits for
   a decision, an edit of the review once it was posted, and, for a person's request
   on a PR with no review, a comment drafted for them. Why a run did not happen is
   the job's result.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    ActionStatus,
    CommentUpsertPayload,
    LabReproducePayload,
    Proposal,
    ProposalState,
    Provenance,
    ReviewPostPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.diffs import commentable
from ..domain.events import Event, EventKind
from ..domain.labs import setup_lines, time_limit, unusable
from ..domain.records import ActionRecord
from ..domain.snapshot import PrSnapshot, PrState
from .base import (
    Playbook,
    PlaybookContext,
    PlaybookResult,
    Wait,
    keeps_session,
)
from .prompts import (
    pr_block,
    reproduction_block,
    review_skill_path,
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
from .review_session import held_gpus, machine_facts

WAIT_SECONDS = 7 * 24 * 3600

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to write its report."""

SESSION = """You are the reproduction agent of the review of PR #{number} in {repo}, inside an automated maintainer service, working on test machine {lab}.
- {skill}
- {asked}
- When you are done, return the JSON your task asks for: the conclusion (reproduced, not_reproduced, or inconclusive), in `comment` the record your skill has the reproduction agent report, in {public_language}, and in `notes` anything only maintainers should read."""

SKILL = (
    "Your job is the reproduction agent that the review skill at `{path}` describes: "
    "read that skill (its part on reproducing a PR, and the review questions the "
    "record answers) and do what it says the reproduction agent does."
)

ASK = "When only {by} can settle something that changes what you run, ask them with your tool for asking the user."

UPDATE = """You wrote the {what} of PR #{number} in {repo} given below, and it said a reproduction on a test machine would follow. That reproduction has ended; its record is below. Revise the {what} with it:
- Fill in where the skill asks how the PR was reproduced, what came out, and whether the documentation could be followed: the steps run and how, the functional and the metric result, the first failure and every step the docs left out; or, when no run happened, why and what a run would need.
- Change a finding or the recommendation only where the run settles it: it confirmed or disproved a claim, or the documented steps failed.
- Keep everything else word for word, including edits the reviewers made to the draft by hand.
- Return the whole revised {what} in `{field}`, and say in `reply`, in a sentence or two, what you changed.
- The {what} is public, in {public_language}: machine names, scores, and other internal details never go in it."""


class ReproReport(BaseModel):
    conclusion: Literal["reproduced", "not_reproduced", "inconclusive"]
    comment: str = Field(
        description="The record the review skill has the reproduction agent report."
    )
    notes: str = Field(default="", description="For maintainers only.")


def _request(ctx: PlaybookContext, event: Event) -> Event:
    """The request that started the job: its first event (later ones only wake it)."""
    return Event.model_validate(ctx.job.events[0]) if ctx.job.events else event


class ReproducePlaybook(Playbook):
    name = "reproduce"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event, profile) -> bool:  # type: ignore[no-untyped-def]
        return (
            event.payload.get("command") == "reproduce"
            and bool(event.payload.get("by") or event.payload.get("brief"))
            and bool(event.payload.get("lab"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        return f"reproduce:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        request = _request(ctx, event)
        if request.kind == EventKind.REVIEW_REPRODUCE:
            return PlaybookResult(
                result={
                    "skipped": "a review's reproduction from before reviews tested "
                    "their PRs in their own session; ask for one again if needed"
                }
            )
        assert request.repo and request.number
        by: str | None = request.payload.get("by") or None
        lab = str(request.payload["lab"])
        ctx.requested_by = by
        ctx.agent = request.payload.get("agent") or None
        discriminator = f"req:{request.id}"
        mine = [
            r
            for r in await ctx.store.list_actions(
                repo=request.repo,
                number=request.number,
                kinds=[
                    ActionKind.LAB_REPRODUCE,
                    ActionKind.LAB_REVIEW,
                    ActionKind.COMMENT_UPSERT,
                ],
            )
            if r.action.discriminator == discriminator
        ]
        planned = next(
            (
                r
                for r in mine
                if r.action.kind in (ActionKind.LAB_REPRODUCE, ActionKind.LAB_REVIEW)
            ),
            None,
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
        why = unusable(pr.repo, lab, machine, binding, by)
        if why or machine is None:
            return await self._not_run(
                ctx, request, pr, why or f"{lab} is not set up", reason=why
            )
        ctx.trusted_input = await ctx.author_is_team(pr)
        action = _session_action(
            pr,
            lab=lab,
            limit=time_limit(machine, binding),
            by=by,
            brief=str(request.payload.get("brief") or "").strip(),
            note=str(request.payload.get("note") or "").strip(),
            discriminator=discriminator,
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
                reason=f"waiting for a session on {lab}",
                action_ids=[action.id],
            ),
            result={"action": action.id},
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
                    "the session on the machine did not open in time",
                    reason="the session did not open in time",
                )
            return PlaybookResult(
                wait=Wait(
                    kinds=[
                        EventKind.ACTION_EXECUTED,
                        EventKind.ACTION_FAILED,
                        EventKind.PROPOSAL_DECIDED,
                    ],
                    seconds=WAIT_SECONDS,
                    reason=f"waiting for a session on {lab}",
                    action_ids=[record.action.id],
                ),
                result={"action": record.action.id},
            )
        if record.status == ActionStatus.DENIED:
            return await self._not_run(
                ctx,
                request,
                pr,
                "the repository's settings do not let it run on a test machine",
                reason="the repository's settings refused it",
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
                        "brief": payload.brief or payload.summary,
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
        if request.payload.get("review"):
            # Its review was turned down while it ran: the record stays with the job.
            return PlaybookResult(result={**summary, "comment": report.comment})
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
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo,
            pr.number,
            record.action.head_sha or pr.head_sha,
            info.default_branch,
        )
        note = str(request.payload.get("note") or "").strip()
        asked = (
            "The reviewer's brief, which you follow:\n"
            + untrusted("review.brief", payload.brief, 8_000)
            if payload.brief
            else f"{by} asked for this reproduction"
            + (
                "; what they want checked:\n"
                + untrusted("maintainer.note", note, 2_000)
                if note
                else ": reproduce what the PR adds or claims, as your skill says."
            )
        )
        prompt = "\n\n".join(
            part
            for part in [
                SESSION.format(
                    number=pr.number,
                    repo=pr.repo,
                    lab=lab,
                    skill=SKILL.format(
                        path=review_skill_path(profile, ctx.service.skills_dir, workdir)
                    ),
                    asked=asked,
                    public_language=profile.languages.public,
                ),
                ASK.format(by=by) if by else "",
                machine_facts(
                    lab,
                    machine.description if machine else "",
                    held_gpus(record.external_ref),
                    setup_lines(machine, profile.labs.get(lab)) if machine else None,
                    payload.timeout_seconds // 60,
                ),
                pr_block(pr),
                schema_hint(ReproReport.model_json_schema()),
            ]
            if part
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
                    # The agent may use the whole session, then write its report.
                    min_timeout_seconds=max(
                        payload.timeout_seconds,
                        ctx.lab_sessions.seconds_left(session_id),
                    )
                    + SESSION_GRACE_SECONDS,
                    lab_session=token,
                    env={"AM_LAB_SOCKET": str(ctx.service.lab_socket or "")},
                ),
                ReproReport,
            )
        finally:
            await ctx.workspace.release(workdir)
            keep = keeps_session(ctx, answer)
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
        **extra: object,
    ) -> PlaybookResult:
        """End without a run. For a review, its draft says why and what a run would
        need; for a maintainer, the job's result does."""
        return PlaybookResult(result={"skipped": reason or why, **extra})

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
            commentable(diff),
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


def _session_action(
    pr: PrSnapshot,
    *,
    lab: str,
    limit: int,
    by: str | None,
    brief: str,
    note: str,
    discriminator: str,
    team: bool,
) -> Action:
    """The session the reproduction agent works in. A review's (``brief``) is a
    ``lab.review``, under the rule the review's own session runs by; a person's is a
    ``lab.reproduce`` drafted for them, which their request approves
    (``pipeline.RUN_ON_REQUEST``)."""
    summary = (brief or note or f"Reproduce #{pr.number}").strip().splitlines()[0]
    return make_action(
        ActionKind.LAB_REVIEW if brief else ActionKind.LAB_REPRODUCE,
        pr.repo,
        LabReproducePayload(
            lab=lab,
            summary=summary[:300],
            brief=brief,
            wants_gpu=True,
            timeout_seconds=limit,
            sides=["base", "head"],
            requested_by=by,
        ),
        number=pr.number,
        head_sha=pr.head_sha,
        discriminator=discriminator,
        rationale=f"a reproduction on {lab}"
        + (", from its review's brief" if brief else f", asked by {by}"),
        hints=ActionHints(author_is_team=team, changed_paths=pr.paths),
        provenance=Provenance(requested_by=by),
        draft_for=None if brief else by,
    )
