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

"""Reviewers talking with the agent about its review (design 5.13), and with the agent
that changed a PR about its change (``web/fix_talk.py``), on the same endpoints.

A review or follow-up job has one conversation, shared by everyone who may see its
repository and kept on the job's live view. While the agent writes the review, a
message goes to it at work (``AgentRunner.tell``), and the review it finishes answers
it. Once the review has a draft, reviewers ask why the agent found something, push
back, have it change the draft, or have it check more. Each such turn runs an agent in
a checkout of the reviewed commit, with the PR, the repository's review skill, the
review as it stands (reviewers' edits included), and the conversation so far, since
not every agent CLI can resume a session. A turn that changes the review returns it
whole: the service renders it as the playbook would. On a pending draft it is saved as
an edit by the reviewer who asked; on a review already posted it becomes a
``review.update`` draft that edits the posted text, as whoever posted it, once
approved. Nothing is posted without a decision. Questions the agent asks go to that
reviewer, on the same view.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionKind,
    ActionStatus,
    ProposalState,
    ReviewPostPayload,
    ReviewUpdatePayload,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.diffs import commentable
from ..domain.events import Event, EventKind
from ..domain.ids import new_id
from ..domain.labs import first_usable
from ..domain.memory import SignalKind
from ..domain.profile import RepoProfile, Route
from ..domain.records import ActionRecord, ChatMessage, ChatThread, Job
from ..domain.secrets import redact_secrets
from ..domain.snapshot import PrSnapshot
from ..playbooks.base import author_is_team
from ..playbooks.person_notes import load_notes, notes_block, remember_said
from ..playbooks.prompts import reproduction_block, schema_hint, told_block
from ..playbooks.review import reproduction_record
from ..playbooks.review_revise import (
    answer_model,
    apply_revision,
    names,
    owns_format,
    posted_update,
    posted_update_action,
    revision_blocks,
    task_kind_of,
)
from ..service import Service
from .api import is_maintainer, service_of
from .auth import User, current_user
from .chat import (
    ProposedAction,
    apply_thread_agent,
    check_agent,
    keep_session,
    save_lesson,
    stopped_message,
    turn_route,
)
from .fix_talk import fix_draft, fix_turn, pending
from .jobs import ACTIVE, _job, _payload
from .live_turns import LiveTurns, Stopped
from .records import open_door, reading_line

log = logging.getLogger(__name__)

TALKS_ON = {"review", "followup", "fix"}
"""Playbooks whose jobs have a conversation."""

TALK_DIFF_LIMIT = 40_000
"""The diff a turn carries; the agent reads the rest in its checkout. A turn's
prompt stays far below what any agent CLI takes in one message."""

_turns: dict[str, asyncio.Lock] = {}
"""One turn at a time per conversation, in this process."""


TALK = """You wrote the {what} of PR #{number} in {repo} given below, and {user}, a reviewer, is talking with you about it. They may ask why you found something, disagree with a finding, ask you to change the draft, or ask you to check more: other files, a test, the docs, a scenario.
- Answer in `reply`, plainly and briefly, in the language {user} writes in; point to code as `path:line`.
- Check before you answer: read the code in the working directory and its git history. Never post, push, or change the repository's files.
- Never run code on this machine: not the PR's code, its tests, builds, installs, or its workflow. A repository's code runs only on its test machines, through the service's reproduction. When {user} asks for a run, or agrees to one that would settle the question, put what it should check in `reproduce`: the service starts the review skill's reproduction agent on the repository's test machine at once, and its record is added to the {what}. Say so in `reply`.
- When {user} asks for a change to the {what}, or what you check changes it, return the whole revised {what} in `{field}`: every finding, section, and line that should stay, word for word, including edits the reviewers made to the draft by hand. Leave `{field}` null when it stays as it is.
- When only {user} can settle something that would change your answer, ask them with your tool for asking the user.
- Put a rule in `lessons` only when {user} states one for future reviews of this repository or agrees to one.
- The {what} is public, in {public_language}; scores, tiers, and other internal priorities never go in it."""


class TalkBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    agent: str | None = None


def thread_id_of(job: Job) -> str:
    return f"chat_{job.id}"


def _posted(record: ActionRecord) -> int | None:
    """The id of the review ``record`` posted on GitHub, if it was posted."""
    if record.status != ActionStatus.EXECUTED:
        return None
    review_id = record.external_ref.get("review_id")
    return int(review_id) if review_id else None


async def _revision(service: Service, record: ActionRecord) -> ActionRecord | None:
    """The pending ``review.update`` draft that edits the review ``record`` posted."""
    for other in await service.store.list_actions(
        repo=record.action.repo,
        number=record.action.number,
        kinds=[ActionKind.REVIEW_UPDATE],
        limit=200,
    ):
        if other.action.payload.get("revises") != record.action.id:
            continue
        proposal = await service.store.proposal_for_action(other.action.id)
        if proposal is not None and proposal.state == ProposalState.PENDING:
            return other
    return None


async def _draft(service: Service, job: Job) -> ActionRecord | None:
    """The review draft ``job`` produced, if it produced one."""
    for record in await service.store.list_actions(
        repo=job.repo, number=job.number, kinds=[ActionKind.REVIEW_POST], limit=200
    ):
        if record.action.provenance.job_id == job.id:
            return record
    return None


def _route(
    service: Service, profile: RepoProfile, kind: TaskKind, ran_on: str | None
) -> Route:
    """The review's route, on the agent that wrote the review while it is ready."""
    route = profile.route(str(kind), service.info.routing)
    if route is None:
        raise HTTPException(500, f"no agent route for {kind}")
    if ran_on and service.agent_ready(ran_on) and ran_on != route.primary:
        others = [a for a in [route.primary, *route.fallback] if a != ran_on]
        route = route.model_copy(update={"primary": ran_on, "fallback": others})
    return route


async def _checkout(
    service: Service, thread: ChatThread, record: ActionRecord, pr: PrSnapshot
) -> tuple[Path, str]:
    """The conversation's checkout of the reviewed commit, kept for the whole
    conversation so a session can resume there, and the default branch."""
    info = await service.a.forge.get_repo(pr.repo)
    if thread.workdir and Path(thread.workdir).is_dir():
        return Path(thread.workdir), info.default_branch
    path = await service.a.workspace.prepare(
        pr.repo,
        pr.number,
        record.action.head_sha or pr.head_sha,
        info.default_branch,
    )
    thread.workdir = str(path)
    thread.session = None
    await service.store.save_thread(thread)
    return path, info.default_branch


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/jobs")

    async def talk_job(service: Service, user: User, job_id: str) -> Job:
        job = await _job(service, user, job_id)
        if (
            job.playbook not in TALKS_ON
            or not job.repo
            or (job.number is None and job.playbook != "fix")
        ):
            raise HTTPException(
                404, "only a review, a follow-up, or a fix has a conversation"
            )
        return job

    @r.get("/{job_id}/talk")
    async def conversation(
        request: Request, job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The conversation about this review: its messages, the run answering now,
        and the draft it talks about (whether it can still change)."""
        service = service_of(request)
        job = await talk_job(service, user, job_id)
        thread = await service.store.get_thread(thread_id_of(job))
        if job.playbook == "fix":
            record = await fix_draft(service, job)
            return {
                "subject": "fix",
                "thread": thread.id if thread else None,
                "agent": thread.agent if thread else None,
                "runtime": (
                    thread.runtime
                    if thread and thread.runtime
                    else (record.action.payload.get("runtime") if record else None)
                ),
                "live_run": thread.live_run if thread else None,
                "listening": service.a.agents.listening(job.id),
                "posted": False,
                "revision": None,
                "messages": [
                    m.model_dump(mode="json")
                    for m in (
                        await service.store.list_messages(thread.id) if thread else []
                    )
                ],
                "draft": record.action.id if record else None,
                "draft_pending": await pending(service, record) is not None,
                "open": job.status not in ACTIVE,
            }
        record = await _draft(service, job)
        proposal = (
            await service.store.proposal_for_action(record.action.id)
            if record
            else None
        )
        revision = await _revision(service, record) if record else None
        return {
            "subject": "review",
            "thread": thread.id if thread else None,
            "agent": thread.agent if thread else None,
            "runtime": (
                thread.runtime
                if thread and thread.runtime
                else (record.action.payload.get("runtime") if record else None)
            ),
            "live_run": thread.live_run if thread else None,
            "listening": service.a.agents.listening(job.id),
            "posted": bool(record and _posted(record)),
            "revision": revision.action.id if revision else None,
            "messages": [
                m.model_dump(mode="json")
                for m in (
                    await service.store.list_messages(thread.id) if thread else []
                )
            ],
            "draft": record.action.id if record else None,
            "draft_pending": bool(proposal and proposal.state == ProposalState.PENDING),
        }

    @r.get("/{job_id}/talk/steps")
    async def turn_steps(
        request: Request,
        job_id: str,
        run: str | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """What the agent did for one answer (``run``), or is doing now."""
        service = service_of(request)
        job = await talk_job(service, user, job_id)
        thread = await service.store.get_thread(thread_id_of(job))
        if thread is None:
            return {"run": None, "live": False, "steps": []}
        if run is None:
            run = thread.live_run
        elif run != thread.live_run and not any(
            m.run_id == run for m in await service.store.list_messages(thread.id)
        ):
            raise HTTPException(404, "no such run in this conversation")
        if run is None:
            return {"run": None, "live": False, "steps": []}
        return {
            "run": run,
            "live": run == thread.live_run,
            "steps": [
                s.model_dump(mode="json") for s in await service.progress.steps(run)
            ],
        }

    @r.post("/{job_id}/talk/stop")
    async def stop(
        request: Request, job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Stop the agent answering in a review's conversation: the review's asker
        or a maintainer, as for a turn. The review's own agent stops from its job."""
        service = service_of(request)
        job = await talk_job(service, user, job_id)
        assert job.repo
        asker = str(_payload(job).get("by") or "")
        if asker.lower() != user.login.lower() and not await is_maintainer(
            service, user, job.repo
        ):
            raise HTTPException(403, "only the asker and the maintainers stop it")
        thread = await service.store.get_thread(thread_id_of(job))
        live = request.app.state.live_turns
        return {
            "stopped": await live.stop(thread.live_run if thread else None, user.login)
        }

    @r.post("/{job_id}/talk")
    async def say(
        request: Request,
        job_id: str,
        body: TalkBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """One turn: the reviewer's message and the agent's answer, which may revise
        the pending draft (saved as the reviewer's edit) or the posted review (a
        ``review.update`` draft) and save lessons. While the review is being written,
        the message goes to the agent at work instead."""
        service = service_of(request)
        job = await talk_job(service, user, job_id)
        assert job.repo
        asker = str(_payload(job).get("by") or "")
        if asker.lower() != user.login.lower() and not await is_maintainer(
            service, user, job.repo
        ):
            # A turn can revise the draft, so it is the asker's or a maintainer's.
            what = "change" if job.playbook == "fix" else "review"
            raise HTTPException(
                403,
                f"{asker or 'the service'} asked for this {what}; only they and the "
                f"maintainers of {job.repo} talk to its agent",
            )
        if body.agent:
            check_agent(service, body.agent)
        if service.a.agents.listening(job.id):
            return await _tell(service, user, job, body.text, body.agent)
        if job.playbook == "fix":
            return await _fix_say(request, service, user, job, body)
        assert job.number is not None
        record = await _draft(service, job)
        if record is None:
            raise HTTPException(
                409,
                "the agent cannot take a message now: it is between steps, or its CLI "
                "cannot read one while it works. Send it once the review has a draft.",
            )
        profile = await service.profiles.active(job.repo)
        pr = await service.a.forge.get_pr(job.repo, job.number)
        if profile is None or pr is None:
            raise HTTPException(409, f"{job.repo}#{job.number} is not available now")
        lock = _turns.setdefault(thread_id_of(job), asyncio.Lock())
        if lock.locked():
            # A new message interrupts the turn still running.
            running = await service.store.get_thread(thread_id_of(job))
            if running is not None:
                await request.app.state.live_turns.stop(running.live_run, user.login)
        async with lock:
            return await _turn(
                service,
                user,
                job,
                record,
                profile,
                pr,
                body.text,
                getattr(request.app.state, "records", None),
                getattr(request.app.state, "live_turns", None),
                agent=body.agent,
            )

    async def _fix_say(
        request: Request, service: Service, user: User, job: Job, body: TalkBody
    ) -> dict[str, Any]:
        assert job.repo
        if job.status in ACTIVE:
            raise HTTPException(
                409,
                "the agent cannot take a message now: it is between steps, or its CLI "
                "cannot read one while it works. Send it once the change is drafted.",
            )
        profile = await service.profiles.active(job.repo)
        pr = (
            await service.a.forge.get_pr(job.repo, job.number)
            if job.number is not None
            else None
        )
        if profile is None or (job.number is not None and pr is None):
            target = f"{job.repo}#{job.number}" if job.number is not None else job.repo
            raise HTTPException(409, f"{target} is not available now")
        record = await fix_draft(service, job)
        route = _route(
            service,
            profile,
            TaskKind.FIX,
            record.action.payload.get("runtime") if record else None,
        )
        lock = _turns.setdefault(thread_id_of(job), asyncio.Lock())
        if lock.locked():
            running = await service.store.get_thread(thread_id_of(job))
            if running is not None:
                await request.app.state.live_turns.stop(running.live_run, user.login)
        async with lock:
            thread = await service.store.get_thread(thread_id_of(job)) or ChatThread(
                id=thread_id_of(job),
                owner=user.login,
                title=f"{job.repo}#{job.number}"
                if job.number is not None
                else job.repo,
                repo=job.repo,
                number=job.number,
                job_id=job.id,
            )
            default_agent = await service.runs_on_for(route, user.login, "fix")
            apply_thread_agent(service, thread, body.agent, default_agent=default_agent)
            await service.store.save_thread(thread)
            return await fix_turn(
                service,
                user,
                job,
                thread,
                record,
                profile,
                pr,
                body.text,
                route,
                getattr(request.app.state, "records", None),
                getattr(request.app.state, "live_turns", None),
            )

    return r


async def _tell(
    service: Service,
    user: User,
    job: Job,
    said: str,
    agent: str | None = None,
) -> dict[str, Any]:
    """Bring a reviewer's message to the agent writing the review; the review it
    finishes answers it, and later turns read it in the conversation."""
    assert job.repo
    text, removed = redact_secrets(said)
    if not service.a.agents.tell(job.id, user.login, told_block(user.login, text)):
        raise HTTPException(409, "the agent finished this step; send it again")
    thread = await service.store.get_thread(thread_id_of(job)) or ChatThread(
        id=thread_id_of(job),
        owner=user.login,
        title=f"{job.repo}#{job.number}" if job.number is not None else job.repo,
        repo=job.repo,
        number=job.number,
        job_id=job.id,
    )
    apply_thread_agent(service, thread, agent)
    await service.store.save_thread(thread)
    message = ChatMessage(
        thread_id=thread.id,
        role="user",
        text=text,
        author=user.login,
        while_running=True,
    )
    await service.store.add_message(message)
    await remember_said(
        service.store,
        user.login,
        text,
        repo=job.repo,
        number=job.number,
        where="fix discussion" if job.playbook == "fix" else "review discussion",
        kind=SignalKind.REVIEW_DISCUSSION,
        message_id=message.id,
    )
    if removed:
        await service.store.add_message(
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                text="I removed what looked like a credential from your message "
                "before the agent read it.",
            )
        )
    return message.model_dump(mode="json")


async def _turn(
    service: Service,
    user: User,
    job: Job,
    record: ActionRecord,
    profile: RepoProfile,
    pr: PrSnapshot,
    said: str,
    door: Any = None,
    live: LiveTurns | None = None,
    *,
    agent: str | None = None,
) -> dict[str, Any]:
    assert job.repo and job.number is not None
    thread = await service.store.get_thread(thread_id_of(job)) or ChatThread(
        id=thread_id_of(job),
        owner=user.login,
        title=f"{job.repo}#{job.number}",
        repo=job.repo,
        number=job.number,
        job_id=job.id,
    )
    payload = ReviewPostPayload.model_validate(record.action.payload)
    posted = _posted(record)
    revision = await _revision(service, record) if posted else None
    if revision is not None:
        # The review as it stands is the revision waiting to be posted.
        pending = ReviewUpdatePayload.model_validate(revision.action.payload)
        payload = payload.model_copy(
            update={"body": pending.body, "result": pending.result or payload.result}
        )
    kind = task_kind_of(payload)
    base_route = _route(service, profile, kind, payload.runtime)
    default_agent = await service.runs_on_for(base_route, user.login, str(kind))
    apply_thread_agent(service, thread, agent, default_agent=default_agent)
    text, removed = redact_secrets(said)
    await service.store.save_thread(thread)
    said = ChatMessage(thread_id=thread.id, role="user", text=text, author=user.login)
    await service.store.add_message(said)
    await remember_said(
        service.store,
        user.login,
        text,
        repo=job.repo,
        number=job.number,
        where="review discussion",
        kind=SignalKind.REVIEW_DISCUSSION,
        action_kind=record.action.kind,
        message_id=said.id,
    )
    history = await service.store.list_messages(thread.id)
    head = record.action.head_sha or pr.head_sha
    workdir, default_branch = await _checkout(service, thread, record, pr)
    diff = await service.a.workspace.diff(workdir, f"origin/{default_branch}", head)
    own_format = owns_format(kind, profile, workdir)
    model = answer_model(kind)
    what, field = names(kind)
    lessons = await service.knowledge.review_lessons(job.repo, profile)
    prompt = "\n\n".join(
        part
        for part in [
            TALK.format(
                what=what,
                number=job.number,
                repo=job.repo,
                user=user.login,
                field=field,
                public_language=profile.languages.public,
            ),
            # The reviewers' words come first: they are the task, and a CLI that
            # cuts a long prompt short keeps its start.
            "The conversation so far:\n"
            + (
                "\n".join(
                    f"{m.author or 'a reviewer'}: {m.text[:4_000]}"
                    if m.role == "user"
                    else f"you (the agent): {m.text[:4_000]}"
                    for m in history[-20:-1]
                )
                or "(this is the first message)"
            ),
            f"The newest message, from {user.login}, which you answer:\n{text}",
            *revision_blocks(
                kind=kind,
                profile=profile,
                skills_dir=service.info.skills_dir,
                workdir=workdir,
                reproduction=reproduction_block(
                    await reproduction_record(service.store, job.repo, job.number)
                ),
                lessons=lessons,
                pr=pr,
                head=head,
                default_branch=default_branch,
                diff=diff,
                payload=payload,
                diff_limit=TALK_DIFF_LIMIT,
                reviewer=notes_block(
                    await load_notes(service.store, record.action.draft_for),
                    "reviews",
                )
                if record.action.draft_for
                else "",
            ),
            notes_block(await load_notes(service.store, user.login), "conversations"),
            reading_line(user.login) if door is not None else "",
            schema_hint(model.model_json_schema()),
            f"Answer the newest message, from {user.login}, above.",
        ]
        if part
    )
    route, resume = await turn_route(service, user.login, thread, base_route)
    run = new_id("run")
    token, door_env = await open_door(door, user.login)
    task = AgentTask(
        kind=kind,
        activity="review_talk",
        prompt=prompt,
        workdir=workdir,
        output_schema=model.model_json_schema(),
        skill_dirs=[service.info.skills_dir, *service.info.pack_skill_dirs],
        repo=job.repo,
        pr=job.number,
        job_id=run,
        requested_by=user.login,
        agent_chosen=bool(thread.agent),
        untrusted_input=not await author_is_team(service.a.forge, profile, pr),
        resume_session=resume,
        records_token=token,
        env=door_env,
    )
    thread.live_run = run
    await service.store.save_thread(thread)
    try:
        outcome = await (live or LiveTurns()).run(
            run, service.a.agents.run(task, route, model)
        )
        if isinstance(outcome, Stopped):
            return (await stopped_message(service, thread, run, outcome)).model_dump(
                mode="json"
            )
        result = outcome
    finally:
        if door is not None:
            door.revoke(token)
        thread.live_run = None
        await service.store.save_thread(thread)
    if not result.ok or result.structured is None:
        await service.store.add_message(
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                text=f"The agent could not answer: {result.error or 'no answer'}",
                run_id=run,
                runtime=result.runtime,
            )
        )
        if result.error_kind == "limit":
            raise HTTPException(429, str(result.error))
        raise HTTPException(502, f"the agent failed: {result.error}")
    answer = model.model_validate(result.structured)
    keep_session(thread, result)
    await service.store.save_thread(thread)

    notes: list[str] = []
    if removed:
        notes.append(
            "I removed what looked like a credential from your message before reading it."
        )
    revised: list[str] = []
    new_payload = apply_revision(
        answer, payload, profile, pr, head, own_format, commentable(diff)
    )
    if new_payload is not None:
        proposal = await service.store.proposal_for_action(record.action.id)
        if proposal is not None and proposal.state == ProposalState.PENDING:
            try:
                await service.pipeline.save_edits(proposal.id, user.login, new_payload)
                revised.append(record.action.id)
            except ValueError as e:
                notes.append(f"The revision could not be saved on the draft: {e}")
        elif posted is not None:
            revised.append(
                await _revise_posted(
                    service, user, record, revision, posted, new_payload, run
                )
            )
            if new_payload.get("comments") != payload.comments:
                notes.append(
                    "Comments already posted on lines of the diff stay as they are; "
                    "the revision changes the review's text."
                )
        else:
            notes.append(
                "The draft was already decided and not posted, so this revision is "
                "not saved."
            )
    changes = [
        await save_lesson(
            service, user, ProposedAction(kind="lesson", repo=job.repo, lesson=lesson)
        )
        for lesson in answer.lessons
        if lesson.strip()
    ]
    jobs: list[str] = []
    if answer.reproduce and answer.reproduce.strip():
        started, note = await _reproduce(
            service, user, profile, pr, answer.reproduce.strip()
        )
        jobs += [started] if started else []
        notes.append(note)
    message = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        text="\n\n".join([answer.reply, *notes]),
        proposals=revised,
        changes=changes,
        jobs=jobs,
        run_id=run,
        runtime=result.runtime,
    )
    await service.store.add_message(message)
    return message.model_dump(mode="json")


async def _reproduce(
    service: Service, user: User, profile: RepoProfile, pr: PrSnapshot, focus: str
) -> tuple[str | None, str]:
    """Start a reproduction of ``pr`` for ``user``, as the Reproduce button does, on
    the test machine suited to ``focus`` (the reproduction chooses it), checking it. Returns the
    request event's id (the jobs API resolves it) and a note for the reply."""
    if first_usable(service.machines, profile.labs, pr.repo, user.login) is None:
        return None, (
            "No test machine is in use for this repository, so no reproduction "
            "started; a maintainer turns one on in the repository's Test machines."
        )
    event = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=pr.repo,
        number=pr.number,
        sha=pr.head_sha,
        payload={
            "command": "reproduce",
            "by": user.login,
            "note": focus[:2_000],
        },
        source="review-talk",
    )
    await service.bus.publish(event)
    return event.id, (
        f"Started a reproduction on the test machine suited to it, checking: "
        f"{focus[:500]} Its record is added to the review when it is done."
    )


async def _revise_posted(
    service: Service,
    user: User,
    record: ActionRecord,
    revision: ActionRecord | None,
    review_id: int,
    new_payload: dict[str, Any],
    run: str,
) -> str:
    """Draft the edit of a posted review, or update the edit already waiting, and
    return its action id. Whoever posted the review approves it, as GitHub lets only
    a review's author edit it."""
    update = posted_update(record, new_payload)
    if revision is not None:
        proposal = await service.store.proposal_for_action(revision.action.id)
        if proposal is not None and proposal.state == ProposalState.PENDING:
            await service.pipeline.save_edits(
                proposal.id, user.login, update.model_dump(mode="json")
            )
            return revision.action.id
    action = posted_update_action(
        record,
        new_payload,
        discriminator=f"talk:{run}",
        rationale=f"revise the review posted for {user.login} in the conversation",
        draft_for=user.login,
    )
    saved = await service.pipeline.submit(action, force_propose=True)
    return saved.action.id
