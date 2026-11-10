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

"""Talking with the agent that keeps a repository's People record (design 2.6).

Each repository has one conversation on its People tab, shared by everyone who may
see it. Reviewers tell the agent where the process is written, state rules, answer
its questions, name members, or have it check who should change role. Each turn runs
the agent in a checkout of the default branch kept for the conversation, with the
record it builds on (the newest draft that waits for approval, else the record) and
the conversation so far, since not every agent CLI can resume a session. A message
sent while a turn runs goes to the agent at work (``AgentRunner.tell``). A turn that
changes the record drafts a T2 memory change that replaces the waiting draft; a
maintainer of the repository applies it. Questions the agent asks go to the person
who wrote the message, on the same tab.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.forges import slug
from ..domain.governance import Governance, role_changes
from ..domain.ids import new_id
from ..domain.memory import MemoryChangeState
from ..domain.records import ChatMessage, ChatThread
from ..domain.secrets import redact_secrets
from ..playbooks.governance import TALK, PeopleTurn, base_record, record_block, revised
from ..playbooks.prompts import no_code_rule, schema_hint, told_block
from ..service import Service
from .api import _repo, require_view, service_of
from .auth import User, current_user
from .chat import keep_session, stopped_message, turn_route
from .live_turns import LiveTurns, Stopped
from .records import open_door, reading_line

log = logging.getLogger(__name__)

TOPIC = "people"

_turns: dict[str, asyncio.Lock] = {}
"""One turn at a time per conversation, in this process."""


class TalkBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def thread_id_of(repo: str) -> str:
    return "people_" + slug(repo)


async def _thread(service: Service, repo: str, user: User) -> ChatThread:
    return await service.store.get_thread(thread_id_of(repo)) or ChatThread(
        id=thread_id_of(repo),
        owner=user.login,
        title=f"People of {repo}",
        repo=repo,
        topic=TOPIC,
    )


async def _checkout(service: Service, thread: ChatThread, repo: str) -> Path:
    """The conversation's checkout of the default branch, kept between turns so a
    session can resume there."""
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

    @r.get("/{repo:path}/governance/talk")
    async def conversation(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The conversation: its messages, and the run answering now."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        thread = await service.store.get_thread(thread_id_of(repo))
        return {
            "thread": thread.id if thread else None,
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

    @r.get("/{repo:path}/governance/talk/steps")
    async def turn_steps(
        request: Request,
        repo: str,
        run: str | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """What the agent did for one answer (``run``), or is doing now."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        thread = await service.store.get_thread(thread_id_of(repo))
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

    @r.post("/{repo:path}/governance/talk/stop")
    async def stop(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Stop the agent answering in the repository's People conversation."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        thread = await _thread(service, repo, user)
        live = request.app.state.live_turns
        return {"stopped": await live.stop(thread.live_run, user.login)}

    @r.post("/{repo:path}/governance/talk")
    async def say(
        request: Request,
        repo: str,
        body: TalkBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """One turn: the message and the agent's answer, which may draft a revision
        of the record. While a turn runs, the message goes to the agent at work."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        thread = await _thread(service, repo, user)
        lock = _turns.setdefault(thread.id, asyncio.Lock())
        if lock.locked():
            if thread.live_run and service.a.agents.listening(thread.live_run):
                return await _tell(service, user, thread, body.text)
            # A new message interrupts the turn still running.
            await request.app.state.live_turns.stop(thread.live_run, user.login)
        async with lock:
            thread = await _thread(service, repo, user)
            return await _turn(
                service,
                user,
                thread,
                repo,
                body.text,
                getattr(request.app.state, "records", None),
                getattr(request.app.state, "live_turns", None),
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
    said: str,
    door: Any = None,
    live: LiveTurns | None = None,
) -> dict[str, Any]:
    profile = await service.profiles.active(repo)
    assert profile is not None
    text, removed = redact_secrets(said)
    await service.store.save_thread(thread)
    await service.store.add_message(
        ChatMessage(thread_id=thread.id, role="user", text=text, author=user.login)
    )
    history = await service.store.list_messages(thread.id)
    loaded, current, replaced = await base_record(
        service.governance,
        repo,
        await service.store.list_memory_changes(MemoryChangeState.OPEN),
    )
    workdir = await _checkout(service, thread, repo)
    prompt = "\n\n".join(
        [
            TALK.format(
                repo=repo,
                by=user.login,
                no_code=no_code_rule(repo),
                language=profile.languages.internal,
            ),
            # Reviewers' messages are the task, earlier ones included; the
            # repository's documents are what the agent reads for it.
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
            record_block(current, draft=replaced is not None, loaded=loaded),
            *([reading_line(user.login)] if door is not None else []),
            schema_hint(PeopleTurn.model_json_schema()),
            f"Answer the newest message, from {user.login}, above.",
        ]
    )
    run = new_id("run")
    token, door_env = await open_door(door, user.login)
    route = profile.route(str(TaskKind.GOVERNANCE), service.info.routing)
    if route is None:
        raise HTTPException(500, "no agent route for the People record")
    route, resume = await turn_route(service, user.login, thread, route)
    task = AgentTask(
        kind=TaskKind.GOVERNANCE,
        prompt=prompt,
        workdir=workdir,
        output_schema=PeopleTurn.model_json_schema(),
        skill_dirs=[service.info.skills_dir],
        repo=repo,
        job_id=run,
        requested_by=user.login,
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
            run, service.a.agents.run(task, route, PeopleTurn)
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
            )
        )
        raise HTTPException(502, f"the agent failed: {result.error}")
    answer = PeopleTurn.model_validate(result.structured)
    keep_session(thread, result)
    await service.store.save_thread(thread)

    notes: list[str] = []
    if removed:
        notes.append(
            "I removed what looked like a credential from your message before reading it."
        )
    changes: list[str] = []
    if answer.governance is not None:
        change_id = await _draft(
            service, user, repo, loaded.governance, current, replaced, answer, text
        )
        if change_id:
            changes.append(change_id)
    message = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        text="\n\n".join([answer.reply, *notes]),
        changes=changes,
        run_id=run,
    )
    await service.store.add_message(message)
    return message.model_dump(mode="json")


async def _draft(
    service: Service,
    user: User,
    repo: str,
    applied: Governance,
    current: Governance,
    replaced: Any,
    answer: PeopleTurn,
    said: str,
) -> str | None:
    """Draft the revised record as a memory change replacing the waiting draft, and
    return its id; ``None`` when the revision changes nothing."""
    assert answer.governance is not None
    after = revised(
        current,
        answer.governance,
        repo=repo,
        questions=[],
        by=user.login,
        on=service.clock.now().date(),
        reason=said[:300],
    )
    if after.dump() == current.dump():
        return None
    moves = role_changes(applied, after)
    change = service.governance.draft(
        repo,
        after,
        title=f"people: update {repo}'s roles and process"
        + (f" ({len(moves)} role change(s))" if moves else ""),
        description="\n\n".join(
            part
            for part in [
                answer.reply.strip()[:4_000],
                "Questions for the maintainers:\n"
                + "\n".join(f"- {q}" for q in after.open_questions)
                if after.open_questions
                else "",
                f"From the conversation on the People tab; last message by {user.login}.",
            ]
            if part
        ),
        requested_by=user.login,
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
