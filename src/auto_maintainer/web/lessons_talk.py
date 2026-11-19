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

"""Talking with an agent about one of a repository's lessons files (design 5.11).

Each file (review, priority, agent lessons) has one conversation on the repository's
Lessons tab, shared by everyone who may propose changes to the repository. Reviewers
ask the agent to summarize the file, or to reword, merge, regroup, add, or drop
lessons. Each turn runs the agent in a checkout of the default branch kept for the
conversation, with the lessons it builds on (the newest draft nobody approved yet,
else the file) and the conversation so far. A message sent while a turn runs goes to
the agent at work. A turn that changes the lessons drafts a T2 memory change that
replaces the waiting draft; a maintainer applies it on the tab. Questions the agent
asks go to the person who wrote the message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.forges import slug
from ..domain.ids import new_id
from ..domain.lessons import render
from ..domain.memory import MemoryChange, Tier
from ..domain.records import ChatMessage, ChatThread
from ..domain.secrets import redact_secrets
from ..playbooks.lessons_summary import (
    READERS,
    SUMMARY_TITLE,
    TALK,
    LessonsTurn,
    base_lessons,
    lessons_path,
    revised_lessons,
    unchanged,
)
from ..playbooks.person_notes import load_notes, notes_block, remember_said
from ..playbooks.prompts import no_code_rule, schema_hint, told_block, untrusted
from ..profiles.knowledge import LESSON_FILES
from ..service import Service
from .api import _repo, require_propose, require_view, service_of
from .auth import User, current_user
from .chat import (
    apply_thread_agent,
    check_agent,
    keep_session,
    stopped_message,
    turn_route,
)
from .live_turns import LiveTurns, Stopped
from .records import open_door, reading_line

TOPIC = "lessons"

_turns: dict[str, asyncio.Lock] = {}
"""One turn at a time per conversation, in this process."""


class TalkBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    agent: str | None = None


def thread_id_of(repo: str, kind: str) -> str:
    return f"lessons_{kind}_{slug(repo)}"


def _kind(kind: str) -> str:
    if kind not in LESSON_FILES:
        raise HTTPException(404, f"no lessons of kind {kind!r}")
    return kind


async def _thread(service: Service, repo: str, kind: str, user: User) -> ChatThread:
    return await service.store.get_thread(thread_id_of(repo, kind)) or ChatThread(
        id=thread_id_of(repo, kind),
        owner=user.login,
        title=f"{LESSON_FILES[kind][1]} of {repo}",
        repo=repo,
        topic=TOPIC,
    )


async def _checkout(service: Service, thread: ChatThread, repo: str) -> Path:
    """The conversation's checkout of the default branch, kept between turns so a
    session can resume there and lessons can be checked against the code."""
    if thread.workdir and Path(thread.workdir).is_dir():
        return Path(thread.workdir)
    info = await service.a.forge.get_repo(repo)
    path = await service.a.workspace.prepare(
        repo, None, info.default_branch_sha or info.default_branch, info.default_branch
    )
    thread.workdir = str(path)
    thread.session = None
    await service.store.save_thread(thread)
    return Path(path)


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/repos")

    @r.get("/{repo:path}/lessons/{kind}/talk")
    async def conversation(
        request: Request, repo: str, kind: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The conversation about one lessons file: its messages, and the run
        answering now."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_view(service, user, repo)
        thread = await service.store.get_thread(thread_id_of(repo, kind))
        return {
            "thread": thread.id if thread else None,
            "agent": thread.agent if thread else None,
            "runtime": thread.runtime if thread else None,
            "live_run": thread.live_run if thread else None,
            "listening": bool(
                thread
                and thread.live_run
                and service.a.agents.listening(thread.live_run)
            ),
            "messages": [
                m.model_dump(mode="json")
                for m in (
                    await service.store.list_messages(thread.id) if thread else []
                )
            ],
        }

    @r.get("/{repo:path}/lessons/{kind}/talk/steps")
    async def turn_steps(
        request: Request,
        repo: str,
        kind: str,
        run: str | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """What the agent did for one answer (``run``), or is doing now."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_view(service, user, repo)
        thread = await service.store.get_thread(thread_id_of(repo, kind))
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

    @r.post("/{repo:path}/lessons/{kind}/talk/stop")
    async def stop(
        request: Request, repo: str, kind: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Stop the agent answering in this conversation."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_propose(service, user, repo)
        thread = await _thread(service, repo, kind, user)
        live = request.app.state.live_turns
        return {"stopped": await live.stop(thread.live_run, user.login)}

    @r.post("/{repo:path}/lessons/{kind}/talk")
    async def say(
        request: Request,
        repo: str,
        kind: str,
        body: TalkBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """One turn: the message and the agent's answer, which may draft a revision
        of the lessons. While a turn runs, the message goes to the agent at work."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_propose(service, user, repo)
        if body.agent:
            check_agent(service, body.agent)
        if await service.profiles.active(repo) is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        thread = await _thread(service, repo, kind, user)
        lock = _turns.setdefault(thread.id, asyncio.Lock())
        if lock.locked():
            if thread.live_run and service.a.agents.listening(thread.live_run):
                return await _tell(service, user, thread, body.text)
            # A new message interrupts the turn still running.
            await request.app.state.live_turns.stop(thread.live_run, user.login)
        async with lock:
            thread = await _thread(service, repo, kind, user)
            return await _turn(
                service,
                user,
                thread,
                repo,
                kind,
                body.text,
                getattr(request.app.state, "records", None),
                getattr(request.app.state, "live_turns", None),
                agent=body.agent,
            )

    return r


async def _tell(
    service: Service, user: User, thread: ChatThread, said: str
) -> dict[str, Any]:
    text, _ = redact_secrets(said)
    assert thread.live_run
    if not service.a.agents.tell(
        thread.live_run, user.login, told_block(user.login, text)
    ):
        raise HTTPException(409, "the agent just finished; send it again")
    message = ChatMessage(
        thread_id=thread.id,
        role="user",
        text=text,
        author=user.login,
        while_running=True,
    )
    await service.store.add_message(message)
    return message.model_dump(mode="json")


async def _turn(
    service: Service,
    user: User,
    thread: ChatThread,
    repo: str,
    kind: str,
    said: str,
    door: Any = None,
    live: LiveTurns | None = None,
    *,
    agent: str | None = None,
) -> dict[str, Any]:
    profile = await service.profiles.active(repo)
    assert profile is not None
    base_route = profile.route(str(TaskKind.LEARN), service.info.routing)
    if base_route is None:
        raise HTTPException(500, "no agent route for lessons")
    default_agent = await service.runs_on_for(
        base_route, user.login, str(TaskKind.LEARN)
    )
    apply_thread_agent(service, thread, agent, default_agent=default_agent)
    text, removed = redact_secrets(said)
    await service.store.save_thread(thread)
    message = ChatMessage(
        thread_id=thread.id, role="user", text=text, author=user.login
    )
    await service.store.add_message(message)
    heading = LESSON_FILES[kind][1]
    await remember_said(
        service.store,
        user.login,
        text,
        repo=repo,
        where=f"{heading} conversation",
        message_id=message.id,
    )
    history = await service.store.list_messages(thread.id)
    path = lessons_path(repo, kind)
    applied = await service.a.memory.read_text(path) or ""
    current, draft = await base_lessons(service.store, applied, repo, kind)
    workdir = await _checkout(service, thread, repo)
    prompt = "\n\n".join(
        [
            TALK.format(
                heading=heading.lower(),
                repo=repo,
                readers=READERS[kind],
                no_code=no_code_rule(repo),
                language=profile.languages.internal,
            ),
            # Reviewers' messages are the task, earlier ones included.
            "The conversation so far:\n"
            + (
                "\n".join(
                    f"{m.author or 'a reviewer'}: {m.text[:4_000]}"
                    if m.role == "user"
                    else f"you (the agent): {m.text[:4_000]}"
                    for m in history[-30:-1]
                )
                or "(this is the first message)"
            ),
            f"The newest message, from {user.login}, which you answer:\n{text}",
            (
                "The lessons as your last draft left them; it waits for a maintainer "
                "to apply it:"
                if draft is not None
                else "The lessons as they are applied:"
            )
            + "\n"
            + untrusted("lessons", render(current), 40_000),
            *([reading_line(user.login)] if door is not None else []),
            notes_block(await load_notes(service.store, user.login), "conversations"),
            schema_hint(LessonsTurn.model_json_schema()),
            f"Answer the newest message, from {user.login}, above.",
        ]
    )
    run = new_id("run")
    token, door_env = await open_door(door, user.login)
    route, resume = await turn_route(service, user.login, thread, base_route)
    task = AgentTask(
        kind=TaskKind.LEARN,
        activity="lessons_talk",
        prompt=prompt,
        workdir=workdir,
        output_schema=LessonsTurn.model_json_schema(),
        skill_dirs=[service.info.skills_dir],
        repo=repo,
        job_id=run,
        requested_by=user.login,
        agent_chosen=bool(thread.agent),
        untrusted_input=False,
        resume_session=resume,
        messages=True,
        records_token=token,
        env=door_env,
    )
    thread.live_run = run
    await service.store.save_thread(thread)
    try:
        outcome = await (live or LiveTurns()).run(
            run, service.a.agents.run(task, route, LessonsTurn)
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
    answer = LessonsTurn.model_validate(result.structured)
    keep_session(thread, result)
    await service.store.save_thread(thread)

    notes: list[str] = []
    if removed:
        notes.append(
            "I removed what looked like a credential from your message before reading it."
        )
    changes: list[str] = []
    if answer.groups is not None:
        after = revised_lessons(current, answer.groups)
        if not after.items():
            notes.append("I kept the lessons as they were: my revision left none.")
        elif not unchanged(current, after):
            changes.append(
                await _draft(service, user, repo, kind, after, draft, answer)
            )
    reply = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        text="\n\n".join([answer.reply, *notes]),
        changes=changes,
        run_id=run,
        runtime=result.runtime,
    )
    await service.store.add_message(reply)
    return reply.model_dump(mode="json")


async def _draft(
    service: Service,
    user: User,
    repo: str,
    kind: str,
    after: Any,
    replaced: MemoryChange | None,
    answer: LessonsTurn,
) -> str:
    """Draft the revised lessons as a memory change that replaces the waiting draft,
    and return its id. It never merges on its own: a maintainer applies it."""
    dropped = "\n".join(
        f"- {d.lesson.strip()} ({d.why.strip()})" for d in answer.dropped
    )
    change = MemoryChange(
        tier=Tier.T2,
        title=f"{SUMMARY_TITLE} {LESSON_FILES[kind][1].lower()} of {repo}",
        description="\n\n".join(
            part
            for part in [
                answer.reply.strip()[:4_000],
                f"Dropped:\n{dropped}" if dropped else "",
                f"From the conversation on the Lessons tab; last message by {user.login}.",
            ]
            if part
        ),
        files={lessons_path(repo, kind): render(after)},
        repos=[repo],
        requested_by=user.login,
        from_own_signals=False,
        created_at=service.clock.now(),
    )
    await service.store.save_memory_change(change)
    await service.pipeline.submit(
        make_action(
            ActionKind.MEMORY_CHANGE,
            repo,
            MemoryChangePayload(
                change_id=change.id, replaces=replaced.id if replaced else None
            ),
            discriminator=change.id,
        )
    )
    return change.id
