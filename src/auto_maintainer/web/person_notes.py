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

"""A person's own notes on what they want from the agents (``playbooks/person_notes.py``):
each person sees and edits only theirs, turns their learning on or off, and asks it to
learn now."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..playbooks.person_notes import (
    MAX_NOTES,
    NOTE_CHARS,
    About,
    PersonNote,
    backfill_said,
    drafts_progress,
    learn_event,
    load_notes,
    new_signals,
    save_notes,
    teaches,
)
from .api import service_of
from .auth import User, current_user


class NoteBody(BaseModel):
    text: str = Field(max_length=NOTE_CHARS)
    about: About = "both"
    source: str = "person"


class NotesBody(BaseModel):
    learning: bool = True
    notes: list[NoteBody] = Field(default_factory=list, max_length=MAX_NOTES * 2)


async def notes_view(service: Any, login: str) -> dict[str, Any]:
    notes = await load_notes(service.store, login)
    return {
        **notes.model_dump(mode="json"),
        "new_signals": len(await new_signals(service.store, notes)),
        "progress": await drafts_progress(service.store, login, service.clock.now()),
    }


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/me/notes")

    @r.get("")
    async def get_notes(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The person's notes, how many of their signals learning has not read yet,
        and how much of their review drafts they changed before posting."""
        return await notes_view(service_of(request), user.login)

    @r.put("")
    async def put_notes(
        request: Request, body: NotesBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Replace the person's notes and turn their learning on or off. A learned
        note they kept word for word, about the same thing, stays learned, with its
        support; any other is theirs, and learning keeps it as it is."""
        service = service_of(request)
        notes = await load_notes(service.store, user.login)
        learned = {n.text: n for n in notes.notes if n.source == "learned"}
        kept: list[PersonNote] = []
        seen: set[str] = set()
        for n in body.notes:
            text = " ".join(n.text.split())
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            same = learned.get(text)
            kept.append(
                same
                if n.source == "learned" and same is not None and same.about == n.about
                else PersonNote(text=text, about=n.about, source="person")
            )
        if len(kept) > MAX_NOTES * 2:
            raise HTTPException(422, f"at most {MAX_NOTES * 2} notes")
        notes.notes = kept
        notes.learning = body.learning
        await save_notes(service.store, notes)
        return await notes_view(service, user.login)

    @r.post("/learn")
    async def learn_now(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Update the person's notes from what they did since they last learned,
        instead of waiting for enough of it."""
        service = service_of(request)
        notes = await load_notes(service.store, user.login)
        if not notes.learning:
            raise HTTPException(409, "learning is off")
        if not await new_signals(service.store, notes):
            raise HTTPException(409, "nothing new to learn from yet")
        event = learn_event(user.login, notes)
        await service.bus.publish(event)
        return {"event": event.id}

    @r.post("/everyone/learn")
    async def learn_everyone(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """For the owner of the service: keep the conversations people had before
        (once), then update the notes of everyone who has something new to learn
        from and has not turned learning off, now, as their own Learn now would."""
        service = service_of(request)
        decision = await service.service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        backfilled = await backfill_said(service.store)
        people = sorted(
            {
                s.source
                for s in await service.store.list_signals(unconsumed=False)
                if s.source and s.source != "service" and teaches(s)
            },
            key=str.lower,
        )
        started = []
        for login in people:
            notes = await load_notes(service.store, login)
            if notes.learning and await new_signals(service.store, notes):
                await service.bus.publish(learn_event(login, notes))
                started.append(login)
        return {"backfilled": backfilled, "started": started}

    return r
