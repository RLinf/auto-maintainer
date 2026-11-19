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

"""A repository's Lessons tab (design 5.11): its review, priority, and agent lessons,
each with the change that brought it in and who approved that; editing them; and
having an agent summarize a file.

Everyone who may see the repository reads them. An edit, and a summary, is a T2
memory change that never merges on its own: a maintainer's own edit is approved by
making it and applies at once (``ConfigGate``); anyone else's waits for a maintainer,
and so does every summary, since an agent wrote it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.events import Event, EventKind
from ..domain.lessons import LessonsFile, Section, key, parse, render
from ..domain.memory import MemoryChange, MemoryChangeState, Tier
from ..domain.records import AuditEntry
from ..domain.secrets import redact_secrets
from ..playbooks.lessons_summary import COMMAND, SUMMARY_TITLE, lessons_path
from ..profiles.knowledge import LESSON_FILES
from ..service import Service
from .api import _repo, require_propose, require_view, service_of, shows_gate
from .auth import User, current_user

OPEN = (MemoryChangeState.OPEN, MemoryChangeState.CONFLICT)
"""States of a change to a lessons file that still waits."""


class EditBody(BaseModel):
    sections: list[Section] = Field(max_length=100)
    base: str = Field(
        description="The file's text the edit started from: a change since then "
        "refuses the edit, so nobody's lesson is lost unseen."
    )


class SummarizeBody(BaseModel):
    agent: str | None = None


def _kind(kind: str) -> str:
    if kind not in LESSON_FILES:
        raise HTTPException(404, f"no lessons of kind {kind!r}")
    return kind


def _sections(lessons: LessonsFile) -> list[dict[str, Any]]:
    return [{"title": s.title, "items": list(s.items)} for s in lessons.sections]


async def _origins(
    service: Service, path: str, heading: str
) -> dict[str, MemoryChange]:
    """For each lesson (by its words, ``lessons.key``), the merged change it first
    appeared in: changes to a lessons file write the whole file, so a lesson is new
    in the first version that has it after one that did not."""
    merged = [
        c
        for c in await service.store.list_memory_changes(MemoryChangeState.MERGED)
        if c.files.get(path)
    ]
    merged.sort(key=lambda c: c.merged_at or c.created_at)
    origins: dict[str, MemoryChange] = {}
    before: set[str] = set()
    for change in merged:
        now = {key(x) for x in parse(change.files[path] or "", heading).items()}
        for words in now - before:
            origins.setdefault(words, change)
        before = now
    return origins


def _origin(change: MemoryChange | None) -> dict[str, Any] | None:
    if change is None:
        return None
    return {
        "change": change.id,
        "title": change.title,
        "on": (change.merged_at or change.created_at).date().isoformat(),
        "approved_by": sorted({x for xs in change.approvals.values() for x in xs}),
        "asked_by": change.requested_by,
    }


async def _submit(service: Service, repo: str, change: MemoryChange) -> None:
    await service.store.save_memory_change(change)
    await service.pipeline.submit(
        make_action(
            ActionKind.MEMORY_CHANGE,
            repo,
            MemoryChangePayload(change_id=change.id),
            discriminator=change.id,
        )
    )
    await service.actuator.drain()


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/repos")

    @r.get("/{repo:path}/lessons")
    async def lessons(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Each lessons file: its lessons by topic, each with where it came from,
        and the changes to it that wait."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        gate = await shows_gate(service, user, repo, profile) if profile else None
        waiting = [
            c for state in OPEN for c in await service.store.list_memory_changes(state)
        ]
        files = []
        for kind, (_, heading) in LESSON_FILES.items():
            path = lessons_path(repo, kind)
            text = await service.a.memory.read_text(path) or ""
            current = parse(text, heading)
            origins = await _origins(service, path, heading)
            files.append(
                {
                    "kind": kind,
                    "heading": current.heading,
                    "path": path,
                    "text": text,
                    "sections": [
                        {
                            "title": s.title,
                            "items": [
                                {"text": item, "from": _origin(origins.get(key(item)))}
                                for item in s.items
                            ],
                        }
                        for s in current.sections
                    ],
                    "pending": [
                        {
                            "id": c.id,
                            "title": c.title,
                            "description": c.description,
                            "summary": c.title.startswith(SUMMARY_TITLE),
                            "state": c.state,
                            "asked_by": c.requested_by,
                            "created_at": c.created_at.isoformat(),
                            "sections": _sections(parse(c.files[path] or "", heading)),
                        }
                        for c in sorted(waiting, key=lambda c: c.created_at)
                        if c.files.get(path) is not None
                    ],
                }
            )
        return {
            "repo": repo,
            "files": files,
            "can_configure": bool(gate and gate.allowed),
            "has_profile": profile is not None,
        }

    @r.put("/{repo:path}/lessons/{kind}")
    async def edit(
        request: Request,
        repo: str,
        kind: str,
        body: EditBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Save the lessons of one file as edited: a maintainer's edit applies at
        once, anyone else's waits for a maintainer."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_propose(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        path, heading = lessons_path(repo, kind), LESSON_FILES[kind][1]
        text = await service.a.memory.read_text(path) or ""
        if text.strip() != body.base.strip():
            raise HTTPException(
                409, "the lessons changed while you edited them; load them again"
            )
        current = parse(text, heading)
        sections = [
            Section(
                title=redact_secrets(s.title.strip())[0][:200],
                items=[redact_secrets(x.strip())[0] for x in s.items if x.strip()],
            )
            for s in body.sections
        ]
        after = render(LessonsFile(heading=current.heading, sections=sections))
        if after == render(current):
            raise HTTPException(422, "nothing changed")
        change = MemoryChange(
            tier=Tier.T2,
            title=f"lessons: edit {current.heading.lower()} of {repo}",
            description=f"Edited on the Lessons tab by {user.login}.",
            files={path: after},
            repos=[repo],
            author=user.login,
            requested_by=user.login,
            from_own_signals=False,
            created_at=service.clock.now(),
        )
        await service.store.add_audit(
            AuditEntry(
                kind="memory",
                actor=user.login,
                summary=change.title,
                data={"change": change.id},
            )
        )
        await _submit(service, repo, change)
        decision = await service.gate.check(repo, user.login, profile, person=True)
        if decision.allowed:
            await service.approve_memory_change(change.id, user.login)
        stored = await service.store.get_memory_change(change.id)
        return {
            "change": change.id,
            "applied": bool(stored and stored.state == MemoryChangeState.MERGED),
            "state": stored.state if stored else change.state,
        }

    @r.post("/{repo:path}/lessons/{kind}/summarize")
    async def summarize(
        request: Request,
        repo: str,
        kind: str,
        body: SummarizeBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent summarize one file into a draft a maintainer applies; the
        job's live view opens by the returned ``event``."""
        service = service_of(request)
        repo, kind = _repo(repo), _kind(kind)
        await require_propose(service, user, repo)
        if await service.profiles.active(repo) is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        heading = LESSON_FILES[kind][1]
        text = await service.a.memory.read_text(lessons_path(repo, kind)) or ""
        if len(parse(text, heading).items()) < 2:
            raise HTTPException(422, "there is nothing to summarize yet")
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            payload={
                "command": COMMAND,
                "kind": kind,
                "by": user.login,
                "agent": body.agent,
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"event": event.id}

    return r
