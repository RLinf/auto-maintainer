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

"""What the service's agents learned from their own trouble, for its owner (design
5.11): the agents' suggestions of changes to the service, and the lessons about the
service's tools and test machines that wait for the owner's approval.

Only the owner sees and decides them (``code_maintainer``). Approving a suggestion
starts a self-improvement with the text the owner approved, in a conversation of its
own (``chat._start_improve``); its context is that text alone, never what an agent
read. A repository's own lessons wait for its maintainers on the Memory page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.memory import MemoryChangeState
from ..domain.records import ChatMessage, ChatThread
from ..domain.retro import SUGGESTIONS_KEY, Suggestion
from .api import code_maintainer, service_of
from .auth import User, current_user
from .chat import _start_improve


class ApproveBody(BaseModel):
    text: str | None = Field(default=None, max_length=8_000)
    """The change to make, as the owner edited it; the suggestion's own when left out."""


def request_text(s: Suggestion) -> str:
    """What a self-improvement started from ``s`` is asked to do."""
    return f"{s.title}\n\nProblem: {s.problem}\n\nProposed change: {s.proposal}"


async def _owner(service: Any, user: User) -> None:
    allowed, why = await code_maintainer(service, user)
    if not allowed:
        raise HTTPException(403, why or "only the owner of this service decides this")


async def _suggestions(service: Any) -> list[Suggestion]:
    return [
        Suggestion.model_validate(x)
        for x in await service.store.get_state(SUGGESTIONS_KEY) or []
    ]


async def _save(service: Any, items: list[Suggestion]) -> None:
    await service.store.set_state(
        SUGGESTIONS_KEY, [s.model_dump(mode="json") for s in items]
    )


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/learning")

    @r.get("")
    async def view(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The agents' suggestions (open ones first) and the lessons about the
        service's tools and machines waiting for approval; empty for anyone but the
        owner."""
        service = service_of(request)
        allowed, _ = await code_maintainer(service, user)
        if not allowed:
            return {"owner": False, "suggestions": [], "lessons": []}
        items = await _suggestions(service)
        items.sort(key=lambda s: (s.state != "open", -s.last_at.timestamp()))
        lessons = [
            c.model_dump(mode="json")
            for c in await service.store.list_memory_changes(MemoryChangeState.OPEN)
            if c.service and all(p.startswith("global/knowledge/") for p in c.files)
        ]
        return {
            "owner": True,
            "suggestions": [s.model_dump(mode="json") for s in items[:100]],
            "lessons": lessons,
        }

    @r.post("/suggestions/{sid}/approve")
    async def approve(
        request: Request,
        sid: str,
        body: ApproveBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Start a self-improvement with the suggestion (as the owner edited it), in a
        conversation of its own that reports how it goes."""
        service = service_of(request)
        await _owner(service, user)
        if not service.settings.code_repo:
            raise HTTPException(409, "this service has no code repository to change")
        items = await _suggestions(service)
        found = next((s for s in items if s.id == sid), None)
        if found is None:
            raise HTTPException(404, "no such suggestion")
        if found.state != "open":
            raise HTTPException(409, f"this suggestion was already {found.state}")
        text = (body.text or "").strip() or request_text(found)
        thread = ChatThread(owner=user.login, title=f"Suggestion: {found.title}"[:120])
        await service.store.save_thread(thread)
        await service.store.add_message(
            ChatMessage(thread_id=thread.id, role="user", author=user.login, text=text)
        )
        found.improvement = await _start_improve(
            service, user.login, user.login, text, thread.id, []
        )
        found.state, found.decided_by = "approved", user.login
        await _save(service, items)
        return {"suggestion": found.model_dump(mode="json"), "thread": thread.id}

    @r.post("/suggestions/{sid}/dismiss")
    async def dismiss(
        request: Request, sid: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        await _owner(service, user)
        items = await _suggestions(service)
        found = next((s for s in items if s.id == sid), None)
        if found is None:
            raise HTTPException(404, "no such suggestion")
        found.state, found.decided_by = "dismissed", user.login
        await _save(service, items)
        return {"suggestion": found.model_dump(mode="json")}

    return r
