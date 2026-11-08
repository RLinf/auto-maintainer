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

"""Reviewers talking with the agent about its review (design 5.13).

A review or follow-up job has one conversation, shared by everyone who may see its
repository and kept on the job's live view. Reviewers ask why the agent found
something, push back, have it change the draft, or have it check more. Each turn runs
an agent in a checkout of the reviewed commit, with the PR, the repository's review
skill, the review as it stands (reviewers' edits included), and the conversation so
far, since not every agent CLI can resume a session. A turn that changes the review
returns it whole: the service renders it as the playbook would and saves it on the
pending draft as an edit by the reviewer who asked, so the findings a fix or a
follow-up reads change with the text. Nothing is posted; the draft still waits for a
decision. Questions the agent asks go to that reviewer, on the same view.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, ProposalState, ReviewPostPayload
from ..domain.agents import AgentTask, TaskKind
from ..domain.ids import new_id
from ..domain.profile import RepoProfile, Route
from ..domain.records import ActionRecord, ChatMessage, ChatThread, Job
from ..domain.secrets import redact_secrets
from ..domain.snapshot import PrSnapshot
from ..playbooks.base import author_is_team
from ..playbooks.prompts import reproduction_block, schema_hint, untrusted
from ..playbooks.review import reproduction_record
from ..playbooks.review_revise import (
    answer_model,
    apply_revision,
    names,
    owns_format,
    revision_blocks,
    task_kind_of,
)
from ..service import Service
from .api import service_of
from .auth import User, current_user
from .chat import ProposedAction, save_lesson
from .jobs import _job

log = logging.getLogger(__name__)

TALKS_ON = {"review", "followup"}
"""Playbooks whose jobs have a conversation."""

_turns: dict[str, asyncio.Lock] = {}
"""One turn at a time per conversation, in this process."""


TALK = """You wrote the {what} of PR #{number} in {repo} given below, and {user}, a reviewer, is talking with you about it. They may ask why you found something, disagree with a finding, ask you to change the draft, or ask you to check more: other files, a test, the docs, a scenario.
- Answer in `reply`, plainly and briefly, in the language {user} writes in; point to code as `path:line`.
- Check before you answer: read the code in the working directory and its git history. Never post, push, or change the repository's files.
- Never run code on this machine: not the PR's code, its tests, builds, installs, or its workflow, and do not ask to. A repository's code runs only on its test machines, through the service's reproduction; when a run would settle the question, say what it should check (the reviewer can start one with Reproduce).
- When {user} asks for a change to the {what}, or what you check changes it, return the whole revised {what} in `{field}`: every finding, section, and line that should stay, word for word, including edits the reviewers made to the draft by hand. Leave `{field}` null when it stays as it is.
- When only {user} can settle something that would change your answer, ask them with your tool for asking the user.
- Put a rule in `lessons` only when {user} states one for future reviews of this repository or agrees to one.
- The {what} is public, in {public_language}; scores, tiers, and other internal priorities never go in it."""


class TalkBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def thread_id_of(job: Job) -> str:
    return f"chat_{job.id}"


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
        if job.playbook not in TALKS_ON or not job.repo or job.number is None:
            raise HTTPException(404, "only a review or a follow-up has a conversation")
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
        record = await _draft(service, job)
        proposal = (
            await service.store.proposal_for_action(record.action.id)
            if record
            else None
        )
        return {
            "thread": thread.id if thread else None,
            "live_run": thread.live_run if thread else None,
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

    @r.post("/{job_id}/talk")
    async def say(
        request: Request,
        job_id: str,
        body: TalkBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """One turn: the reviewer's message and the agent's answer, which may revise
        the pending draft (saved as the reviewer's edit) and save lessons."""
        service = service_of(request)
        job = await talk_job(service, user, job_id)
        assert job.repo and job.number is not None
        record = await _draft(service, job)
        if record is None:
            raise HTTPException(
                409,
                "this job has no review draft to talk about; it may still be running",
            )
        profile = await service.profiles.active(job.repo)
        pr = await service.a.forge.get_pr(job.repo, job.number)
        if profile is None or pr is None:
            raise HTTPException(409, f"{job.repo}#{job.number} is not available now")
        lock = _turns.setdefault(thread_id_of(job), asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "the agent is still answering the last message")
        async with lock:
            return await _turn(service, user, job, record, profile, pr, body.text)

    return r


async def _turn(
    service: Service,
    user: User,
    job: Job,
    record: ActionRecord,
    profile: RepoProfile,
    pr: PrSnapshot,
    said: str,
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
    text, removed = redact_secrets(said)
    await service.store.save_thread(thread)
    await service.store.add_message(
        ChatMessage(thread_id=thread.id, role="user", text=text, author=user.login)
    )
    history = await service.store.list_messages(thread.id)
    payload = ReviewPostPayload.model_validate(record.action.payload)
    kind = task_kind_of(payload)
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
            ),
            untrusted(
                "conversation",
                "\n".join(
                    f"{m.author or 'a reviewer'}: {m.text}"
                    if m.role == "user"
                    else f"you (the agent): {m.text}"
                    for m in history[-20:]
                ),
                30_000,
            ),
            schema_hint(model.model_json_schema()),
        ]
        if part
    )
    run = new_id("run")
    task = AgentTask(
        kind=kind,
        prompt=prompt,
        workdir=workdir,
        output_schema=model.model_json_schema(),
        skill_dirs=[service.info.skills_dir, *service.info.pack_skill_dirs],
        repo=job.repo,
        pr=job.number,
        job_id=run,
        requested_by=user.login,
        untrusted_input=not await author_is_team(service.a.forge, profile, pr),
        resume_session=thread.session,
    )
    thread.live_run = run
    await service.store.save_thread(thread)
    try:
        result = await service.a.agents.run(
            task, _route(service, profile, kind, payload.runtime), model
        )
    finally:
        thread.live_run = None
        await service.store.save_thread(thread)
    if not result.ok or result.structured is None:
        await service.store.add_message(
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                text=f"The agent could not answer: {result.error or 'no answer'}",
                run_id=run,
            )
        )
        raise HTTPException(502, f"the agent failed: {result.error}")
    answer = model.model_validate(result.structured)
    thread.session = result.session or thread.session
    thread.runtime = result.runtime
    await service.store.save_thread(thread)

    notes: list[str] = []
    if removed:
        notes.append(
            "I removed what looked like a credential from your message before reading it."
        )
    revised = False
    new_payload = apply_revision(answer, payload, profile, pr, head, own_format)
    if new_payload is not None:
        proposal = await service.store.proposal_for_action(record.action.id)
        if proposal is None or proposal.state != ProposalState.PENDING:
            notes.append(
                "The draft was already decided, so this revision is not saved on it."
            )
        else:
            try:
                await service.pipeline.save_edits(proposal.id, user.login, new_payload)
                revised = True
            except ValueError as e:
                notes.append(f"The revision could not be saved on the draft: {e}")
    changes = [
        await save_lesson(
            service, user, ProposedAction(kind="lesson", repo=job.repo, lesson=lesson)
        )
        for lesson in answer.lessons
        if lesson.strip()
    ]
    message = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        text="\n\n".join([answer.reply, *notes]),
        proposals=[record.action.id] if revised else [],
        changes=changes,
        run_id=run,
    )
    await service.store.add_message(message)
    return message.model_dump(mode="json")
