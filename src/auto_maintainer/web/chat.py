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

"""Chat with the service (design 5.13).

Each thread belongs to one maintainer. The agent sees the maintainer's queue and
the memory index and may propose actions; every proposed action becomes a
proposal the maintainer approves in the thread, whatever the policy says.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionKind,
    CommentUpsertPayload,
    LabelPayload,
    MemoryChangePayload,
    NudgePayload,
    RequestReviewersPayload,
    RerunPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, Signal, SignalKind, Tier, repo_dir
from ..domain.records import ChatMessage, ChatThread
from ..playbooks.prompts import schema_hint, untrusted
from .api import require_view, service_of, visible_repos
from .auth import User, current_user


class ProposedAction(BaseModel):
    kind: Literal[
        "label", "rerun", "request_reviewers", "comment", "nudge", "review", "lesson"
    ]
    repo: str
    number: int | None = None
    labels: list[str] = Field(default_factory=list)
    run_id: int | None = None
    logins: list[str] = Field(default_factory=list)
    body: str = ""
    lesson: str = Field(
        default="",
        description="For kind=lesson: a review lesson to remember for this repository.",
    )


class ChatAnswer(BaseModel):
    reply: str
    proposals: list[ProposedAction] = Field(default_factory=list)


class MessageBody(BaseModel):
    text: str
    repo: str | None = None


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/chat")

    @r.get("/threads")
    async def threads(
        request: Request, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        return [
            t.model_dump(mode="json")
            for t in await service.store.list_threads(user.login)
        ]

    @r.post("/threads")
    async def new_thread(
        request: Request, body: MessageBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        if body.repo:
            await require_view(service, user, body.repo)
        thread = ChatThread(owner=user.login, title=body.text[:80], repo=body.repo)
        await service.store.save_thread(thread)
        return thread.model_dump(mode="json")

    @r.get("/threads/{thread_id}/messages")
    async def messages(
        request: Request, thread_id: str, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None or thread.owner != user.login:
            raise HTTPException(404, "unknown thread")
        return [
            m.model_dump(mode="json")
            for m in await service.store.list_messages(thread_id)
        ]

    @r.post("/threads/{thread_id}/messages")
    async def post(
        request: Request,
        thread_id: str,
        body: MessageBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None or thread.owner != user.login:
            raise HTTPException(404, "unknown thread")
        repos = await visible_repos(service, user)
        await service.store.add_message(
            ChatMessage(thread_id=thread_id, role="user", text=body.text)
        )
        history = await service.store.list_messages(thread_id)
        queue = []
        for name in repos[:10]:
            for ev in (await service.store.list_evaluations(name))[:15]:
                queue.append(
                    f"{name}#{ev.number} [{ev.tier} {ev.total}] {ev.title} — {ev.summary}"
                )
        memory_index = await service.knowledge.memory_index(thread.repo)
        prompt = "\n\n".join(
            [
                f"You are the maintainers' assistant for {', '.join(repos) or 'no repositories yet'}. You are talking to {user.login}. "
                "Answer from the data below. You cannot act directly: put any change you suggest in `proposals`; "
                "the maintainer approves each one before it runs. Internal priorities are private: never put them in public text.",
                f"Memory index:\n{memory_index or '(empty)'}",
                "Review queue (internal):\n" + ("\n".join(queue) or "(empty)"),
                untrusted(
                    "chat.history",
                    "\n".join(f"{m.role}: {m.text}" for m in history[-20:]),
                    30_000,
                ),
                schema_hint(ChatAnswer.model_json_schema()),
            ]
        )
        workdir = service.settings.scratch_dir / f"chat-{thread_id}"
        workdir.mkdir(parents=True, exist_ok=True)
        route = service.settings.routing.get("chat")
        if route is None:
            raise HTTPException(500, "no chat route is configured")
        task = AgentTask(
            kind=TaskKind.CHAT,
            prompt=prompt,
            workdir=workdir,
            output_schema=ChatAnswer.model_json_schema(),
            resume_session=thread.session,
        )
        result = await service.a.agents.run(task, route, ChatAnswer)
        if not result.ok or result.structured is None:
            raise HTTPException(502, f"the chat agent failed: {result.error}")
        answer = ChatAnswer.model_validate(result.structured)
        thread.session = result.session or thread.session
        thread.runtime = result.runtime
        await service.store.save_thread(thread)
        created = []
        for p in answer.proposals:
            if p.repo not in repos:
                continue
            created += await _propose(service, user, p)
        message = ChatMessage(
            thread_id=thread_id, role="assistant", text=answer.reply, proposals=created
        )
        await service.store.add_message(message)
        return message.model_dump(mode="json")

    return r


async def _propose(service: Any, user: User, p: ProposedAction) -> list[str]:
    """Turn a chat proposal into actions that wait for the maintainer's approval."""
    if p.kind == "review" and p.number:
        await service.bus.publish(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo=p.repo,
                number=p.number,
                payload={"command": "review", "by": user.login},
                source="chat",
            )
        )
        return []
    if p.kind == "lesson" and p.lesson:
        path = f"{repo_dir(p.repo)}/knowledge/review_lessons.md"
        current = await service.a.memory.read_text(path) or "# Review lessons\n"
        change = MemoryChange(
            tier=Tier.T1,
            title=f"lesson from {user.login}",
            files={path: current.rstrip() + f"\n- {p.lesson.strip()}\n"},
            repos=[p.repo],
            requested_by=user.login,
            created_at=service.clock.now(),
        )
        decision = await service.gate.check(
            p.repo, user.login, await service.profiles.active(p.repo)
        )
        if decision.allowed:
            change.approvals = {p.repo: [user.login]}
        await service.store.save_memory_change(change)
        await service.store.add_signal(
            Signal(
                kind=SignalKind.CHAT_INSTRUCTION,
                repo=p.repo,
                source=user.login,
                payload={"lesson": p.lesson},
            )
        )
        await service.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                p.repo,
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            )
        )
        return []
    pr = await service.a.forge.get_pr(p.repo, p.number) if p.number else None
    if pr is None:
        return []
    disc = f"chat:{service.clock.now().timestamp()}"
    if p.kind == "label":
        action = make_action(
            ActionKind.LABEL_APPLY,
            p.repo,
            LabelPayload(labels=p.labels),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "rerun" and p.run_id:
        action = make_action(
            ActionKind.CI_RERUN_FAILED,
            p.repo,
            RerunPayload(run_id=p.run_id),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "request_reviewers":
        action = make_action(
            ActionKind.PR_REQUEST_REVIEWERS,
            p.repo,
            RequestReviewersPayload(logins=p.logins),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "comment" and p.body:
        action = make_action(
            ActionKind.COMMENT_UPSERT,
            p.repo,
            CommentUpsertPayload(purpose=f"chat-{disc}", body=p.body),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "nudge" and p.body:
        action = make_action(
            ActionKind.PR_NUDGE,
            p.repo,
            NudgePayload(body=p.body),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    else:
        return []
    action.provenance.requested_by = user.login
    action.rationale = f"requested by {user.login} in chat"
    record = await service.pipeline.submit(action, force_propose=True)
    proposal = await service.store.proposal_for_action(record.action.id)
    return [proposal.id] if proposal else []
