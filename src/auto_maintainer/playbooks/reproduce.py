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
starts in one of two ways: a review's agent that judges the PR needs one starts it
with ``lab --reproduce`` and its brief (``Service.start_review_reproduction``), and
the review waits for its record (``review_session.reproduction_outcome``); or a
person asks for one from the PR, naming a machine, or in the conversation about a
review. Nobody approves it first: a review's
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
3. The agent's record goes to the review that started it, which finishes with it;
   for a person's request, into the PR's review: the review draft while it waits for
   a decision, an edit of the review once it was posted (made at once when the
   service posted it or the review's author asked for the run), and, on a PR with no
   review, a comment drafted for them. Why a run did not happen is the job's result,
   and the waiting review's too.
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
from ..domain.records import ActionRecord, JobStatus
from ..domain.snapshot import PrSnapshot, PrState
from .base import (
    Playbook,
    PlaybookContext,
    PlaybookResult,
    Wait,
    keeps_session,
)
from .machine_choice import choose_machine
from .prompts import (
    pr_block,
    reproduction_block,
    review_skill_path,
    schema_hint,
    untrusted,
    work_lessons_block,
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
from .review_session import earlier_work, held_gpus, machine_facts

WAIT_SECONDS = 7 * 24 * 3600

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to write its report."""

SESSION = """You are the reproduction agent of the review of PR #{number} in {repo}, inside an automated maintainer service, working on test machine {lab}.
- {skill}
- {asked}
- {gpus}
- {persistence}
- When you are done, return the JSON your task asks for: the conclusion (reproduced, not_reproduced, inconclusive, or not_needed), in `comment` the record your skill has the reproduction agent report, in {public_language}, and in `notes` anything only maintainers should read."""

SKILL = (
    "Your job is the reproduction agent that the review skill at `{path}` describes: "
    "read that skill (its part on reproducing a PR, and the review questions the "
    "record answers) and do what it says the reproduction agent does."
)

ASK = "When only {by} can settle something that changes what you run, ask them with your tool for asking the user."

GPUS = "Take the GPUs the run needs with `lab --gpus N`. When they are busy, do CPU setup first (venv, downloads) and wait for them (`lab --gpus N --wait MINUTES`, repeating as often as needed — waiting keeps the session alive automatically) rather than run on fewer or report the run as failed for want of them. Only what the machine does not have at all (more GPUs than it has, another node, a robot) is missing; for that, run what your skill says stands in for it on everything the machine has, and record it as an intervention."
"""How the reproduction agent gets its GPUs: it waits for busy ones instead of
giving up, and stands in only for hardware the machine lacks."""

PERSISTENCE = "Do not quit over temporary or solvable blockers: if a venv or package is missing, build/install it in `$AM_LAB_KEEP` (or the machine's venv directory using its configured mirrors); if a checkpoint or dataset download is large or slow, download into `$AM_LAB_KEEP` so partial files resume, use `HF_ENDPOINT` / configured mirrors, retry transient network errors, and avoid short timeouts (active downloads extend the session automatically)."

ALONGSIDE = "The PR's review runs while you work, and your record goes into it. Reproduce what the PR adds or claims, as your skill says; when the skill says the PR needs no reproduction, run nothing and conclude `not_needed`."
"""What the reproduction agent started with a review is asked (``review.alongside``)."""

REVIEW_WAIT_SECONDS = 6 * 3600
"""How long a finished reproduction waits at a time for the review it runs alongside
to be written."""

REPORT_STATE = "reproduce_report:{job}"
"""A finished reproduction's record while it waits for its review's draft."""

RECORD_LIMIT = 40_000
"""Maximum length of the reproduction record passed to the review-revision agent."""

RECORD_COMMAND_CHARS = 1_000
"""Maximum length of each command string kept in the reproduction record."""

RECORD_TAIL_LINES = 30
"""Maximum trailing output lines kept per command in the reproduction record."""

RECORD_TAIL_CHARS = 2_000
"""Maximum total characters of trailing output kept per command in the reproduction record."""

UPDATE = """You are revising the {what} of PR #{number} in {repo} given below. That reproduction has ended; its record is below.

You have the repository checkout here to inspect any file or line the reproduction touched. Decide freely how the {what} as a whole should be updated — do not limit your edits to the reproduction section or do a mechanical text replacement:
- Integrate the reproduction results into where the skill asks how the PR was reproduced, what came out, and whether the documentation could be followed (steps run and settings/adaptations, functional and metric results, first failure, every intervention, or why no run happened).
- If the reproduction uncovered problems (bugs, crashes, missing config or documentation steps, broken scripts, performance regressions, or usability gaps), incorporate them into the whole {what}: add them as findings (with inline file/line comments where applicable) and reflect them in the summary, sections, and overall recommendation.
- If the reproduction confirmed, refined, or disproved existing findings, update, strengthen, or remove those findings and adjust severities and the recommendation as appropriate.
- Keep the {what} coherent as a single unified review (preserving manual edits reviewers made to the draft unless the reproduction directly supersedes them).
- Return the complete revised {what} in `{field}`, and say in `reply`, in a sentence or two, what you changed.
- The {what} is public, in {public_language}: machine names, scores, and other internal details never go in it."""


class ReproReport(BaseModel):
    conclusion: Literal[
        "reproduced", "not_reproduced", "inconclusive", "not_needed"
    ] = Field(
        description="`not_needed` only when the review skill says the PR needs no "
        "reproduction, and nothing ran."
    )
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
            and bool(
                event.payload.get("by")
                or event.payload.get("brief")
                or event.payload.get("alongside")
            )
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
        # The machine the person picked; without one, the playbook chooses.
        lab = str(request.payload.get("lab") or "")
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
            lab = str(planned.action.payload.get("lab") or lab)
            return await self._after_run(ctx, request, event, planned, by, lab)

        pr = await ctx.forge.get_pr(request.repo, request.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if not lab:
            need = str(
                request.payload.get("brief") or request.payload.get("note") or ""
            )
            lab = await choose_machine(ctx, pr, profile, by, need) or ""
            if not lab:
                return await self._not_run(
                    ctx,
                    request,
                    pr,
                    "no test machine may take it",
                    reason="no test machine may take it",
                )
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
            alongside=bool(request.payload.get("alongside")),
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
        kept = await ctx.store.get_state(REPORT_STATE.format(job=ctx.job.id))
        if isinstance(kept, dict):
            # The run is over: its record waits for the review to be written.
            return await self._report(ctx, request, pr, by, kept)
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
            "tested_on": lab,
            "commit": record.action.head_sha,
            "conclusion": report.conclusion,
            "notes": report.notes,
            "commands": [{k: v for k, v in c.items() if k != "tail"} for c in commands],
        }
        if report.conclusion == "not_needed":
            summary_skipped = {**summary, "skipped": "the PR needs no reproduction"}
            if request.payload.get("alongside"):
                record_text = untrusted(
                    "reproduction",
                    "The reproduction agent judged that the PR needs no reproduction "
                    f"and ran nothing: {report.comment}",
                    10_000,
                )
                return await self._report(
                    ctx,
                    request,
                    pr,
                    by,
                    {
                        "summary": summary_skipped,
                        "record_text": record_text,
                        "comment": report.comment,
                        "runtime": runtime,
                        "head_sha": record.action.head_sha,
                    },
                )
            return PlaybookResult(result=summary_skipped)
        machine = ctx.service.machines.get(lab)
        record_text = _record_text(
            machine_description=machine.description if machine else "",
            commit=record.action.head_sha,
            brief=payload.brief or payload.summary,
            conclusion=report.conclusion,
            report=report.comment,
            commands=commands,
        )
        return await self._report(
            ctx,
            request,
            pr,
            by,
            {
                "summary": summary,
                "record_text": record_text,
                "comment": report.comment,
                "runtime": runtime,
                "head_sha": record.action.head_sha,
            },
        )

    async def _report(
        self,
        ctx: PlaybookContext,
        request: Event,
        pr: PrSnapshot | None,
        by: str | None,
        kept: dict[str, Any],
    ) -> PlaybookResult:
        """Put the finished run's record (``kept``) in the PR's review: the review
        that asked for it, or that it runs alongside, once that review is written;
        else, for a person's request, in a comment drafted for them."""
        summary = dict(kept.get("summary") or {})
        alongside = str(request.payload.get("alongside") or "")
        if pr is not None:
            if alongside:
                target = await self._review_of_job(ctx, pr, alongside)
                if target is None and await self._running(ctx, alongside):
                    await ctx.store.set_state(REPORT_STATE.format(job=ctx.job.id), kept)
                    return PlaybookResult(
                        wait=Wait(
                            kinds=[EventKind.JOB_FINISHED],
                            seconds=REVIEW_WAIT_SECONDS,
                            reason="waiting for the review it runs alongside",
                        ),
                        result=summary,
                    )
            else:
                target = await self._review_of(ctx, request, pr)
            record_text = str(kept.get("record_text") or "")
            done: PlaybookResult | None = None
            if target is not None and target[1] is not None:
                revised = await self._revise(ctx, target[0], target[1], pr, record_text)
                if revised is not None:
                    done = PlaybookResult(
                        result={
                            **summary,
                            "revised": revised,
                            **({"for_review": alongside} if alongside else {}),
                        }
                    )
            elif target is not None:
                update = await self._update_posted(
                    ctx, target[0], pr, record_text, by, request
                )
                if update is not None:
                    done = PlaybookResult(
                        actions=[update],
                        result={
                            **summary,
                            "updates": target[0].action.id,
                            **({"for_review": alongside} if alongside else {}),
                        },
                    )
            # Kept until here, so a try that fails while adding it keeps the record.
            await ctx.store.set_state(REPORT_STATE.format(job=ctx.job.id), None)
            if done is not None:
                return done
        if request.payload.get("review") or alongside:
            # Its review was turned down while it ran, or never written: the record
            # stays with the job.
            return PlaybookResult(result={**summary, "comment": kept.get("comment")})
        return PlaybookResult(
            actions=[
                self._comment(
                    request,
                    kept.get("head_sha"),
                    str(kept.get("comment") or ""),
                    by,
                    kept.get("runtime"),
                )
            ],
            result=summary,
        )

    async def _running(self, ctx: PlaybookContext, job_id: str) -> bool:
        job = await ctx.store.get_job(job_id)
        return job is not None and job.status in (
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.WAITING,
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
        brief = (
            "The reviewer's brief, which you follow:\n"
            + untrusted("review.brief", payload.brief, 8_000)
            if payload.brief
            else ""
        )
        asked = (
            "\n".join(x for x in (ALONGSIDE, brief) if x)
            if request.payload.get("alongside")
            else brief
            if brief
            else f"{by} asked for this reproduction"
            + (
                "; what they want checked:\n"
                + untrusted("maintainer.note", note, 2_000)
                if note
                else ": reproduce what the PR adds or claims, as your skill says."
            )
        )
        session_limit = payload.timeout_seconds or (
            time_limit(machine, profile.labs.get(lab)) if machine else 3600
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
                    gpus=GPUS,
                    persistence=PERSISTENCE,
                    public_language=profile.languages.public,
                ),
                ASK.format(by=by) if by else "",
                work_lessons_block(await ctx.knowledge.agent_lessons(pr.repo, [lab])),
                machine_facts(
                    lab,
                    machine.description if machine else "",
                    held_gpus(record.external_ref),
                    setup_lines(machine, profile.labs.get(lab)) if machine else None,
                    session_limit // 60,
                    machine.suited_for if machine else "",
                ),
                await earlier_work(ctx, pr),
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
                    # Allow headroom for large model/dataset downloads plus execution.
                    min_timeout_seconds=max(
                        session_limit * 2,
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
        """End without a run: when started alongside a review, update that review
        with why no run happened; otherwise just record it on the job."""
        summary: dict[str, Any] = {"skipped": reason or why, **extra}
        if request.payload.get("alongside") and pr is not None:
            record_text = untrusted(
                "reproduction", f"The reproduction did not run: {why}.", 10_000
            )
            return await self._report(
                ctx,
                request,
                pr,
                request.payload.get("by"),
                {
                    "summary": summary,
                    "record_text": record_text,
                    "comment": f"The reproduction did not run: {why}.",
                    "runtime": None,
                    "head_sha": pr.head_sha,
                },
            )
        return PlaybookResult(result=summary)

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
        return await self._open_review(ctx, records)

    async def _review_of_job(
        self, ctx: PlaybookContext, pr: PrSnapshot, job_id: str
    ) -> tuple[ActionRecord, Proposal | None] | None:
        """The review the job ``job_id`` wrote, while it waits or once posted."""
        records = [
            r
            for r in await ctx.store.list_actions(
                repo=pr.repo, number=pr.number, kinds=[ActionKind.REVIEW_POST]
            )
            if r.action.provenance.job_id == job_id
        ]
        return await self._open_review(ctx, records)

    async def _open_review(
        self, ctx: PlaybookContext, records: list[ActionRecord]
    ) -> tuple[ActionRecord, Proposal | None] | None:
        """The first of ``records`` that waits for a decision, with its proposal, or
        that was posted."""
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
        """An edit of the posted review that adds the reproduction's record;
        ``None`` when the agent could not revise it. Approved at once when the
        service posted the review or the review's author asked for the reproduction
        (``pipeline.REVISED_ON_REQUEST``); otherwise it waits for whoever posted it."""
        new_payload = await self._revised_payload(ctx, record, pr, record_text)
        if new_payload is None:
            return None
        action = posted_update_action(
            record,
            new_payload,
            discriminator=f"req:{request.id}:review",
            rationale="add the result of a reproduction to the posted review"
            + (f", asked by {by}" if by else ""),
            draft_for=by or record.approver,
        )
        action.provenance.requested_by = by
        return action

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


def _trim_tail(tail: Any) -> Any:
    """Keep the trailing output of one command within per-command limits."""
    if isinstance(tail, str):
        if len(tail) <= RECORD_TAIL_CHARS:
            return tail
        return "[truncated]\n" + tail[-RECORD_TAIL_CHARS:]
    if not isinstance(tail, list):
        return tail
    lines = [str(line) for line in tail]
    kept: list[str] = []
    used = 0
    for line in reversed(lines[-RECORD_TAIL_LINES:]):
        if len(line) > RECORD_TAIL_CHARS:
            if not kept:
                kept.append("..." + line[-(RECORD_TAIL_CHARS - 3) :])
                used = RECORD_TAIL_CHARS
            break
        cost = len(line) + (1 if kept else 0)
        if kept and used + cost > RECORD_TAIL_CHARS:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    omitted = len(lines) - len(kept)
    if omitted > 0:
        kept.insert(0, f"... ({omitted} earlier lines omitted)")
    return kept


def _trim_command(entry: dict[str, Any]) -> dict[str, Any]:
    """Bound ``command`` and ``tail`` of one logged command for the revision prompt."""
    trimmed = dict(entry)
    cmd = trimmed.get("command")
    if isinstance(cmd, str) and len(cmd) > RECORD_COMMAND_CHARS:
        trimmed["command"] = cmd[:RECORD_COMMAND_CHARS] + " ...[truncated]"
    if "tail" in trimmed:
        trimmed["tail"] = _trim_tail(trimmed["tail"])
    return trimmed


def _record_text(
    *,
    machine_description: str,
    commit: str | None,
    brief: str,
    conclusion: str,
    report: str,
    commands: list[dict[str, Any]],
) -> str:
    """Build the untrusted reproduction record for the review-revision agent.

    ``conclusion`` and ``report`` come before ``commands`` (and each command's
    ``command`` and ``tail`` are bounded) so long command logs never push the
    reproduction verdict out of the truncation window.
    """
    return untrusted(
        "reproduction",
        json.dumps(
            {
                "machine": machine_description,
                "commit": commit,
                "brief": brief,
                "conclusion": conclusion,
                "report": report,
                "commands": [_trim_command(c) for c in commands],
            },
            ensure_ascii=False,
            indent=1,
        ),
        RECORD_LIMIT,
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
    alongside: bool = False,
) -> Action:
    """The session the reproduction agent works in. A review's (started with it,
    ``alongside``, or from its ``brief``) is a ``lab.review``, under the rule the
    review's own session runs by; a person's is a ``lab.reproduce`` drafted for them,
    which their request approves (``pipeline.RUN_ON_REQUEST``)."""
    review = bool(brief) or alongside
    summary = (
        (brief or note or f"Reproduce #{pr.number}").strip().splitlines()[0]
        if not alongside
        else f"Reproduce #{pr.number} alongside its review"
    )
    return make_action(
        ActionKind.LAB_REVIEW if review else ActionKind.LAB_REPRODUCE,
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
        + (
            ", alongside its review"
            if alongside
            else ", from its review's brief"
            if brief
            else f", asked by {by}"
        ),
        hints=ActionHints(author_is_team=team, changed_paths=pr.paths),
        provenance=Provenance(requested_by=by),
        draft_for=None if review else by,
    )
