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

"""What each person wants from the service's agents (design 5.11).

A person's notes are a short list of preferences, each about the reviews drafted for
them, about how the agents talk with them, or both. They are learned from everything
the person does with the service: every message they write in a conversation (the
chat, a review's discussion, the People conversation: ``conversation`` and
``review_discussion`` signals), how they edit a review or follow-up draft before
posting it (``draft_edited``), what they turn down and why (``proposal_rejected``),
and what they ask to remember (``chat_instruction``); and from the notes they write
themselves, on the Settings page or by asking in a conversation. Every review,
follow-up, and revision drafted for them reads their notes about reviews; every
conversation with them reads those about conversations. The repository's lessons win
where the two differ. Scans and events work for nobody and read none.

The notes belong to their person, who sees and edits them and turns the learning off
on the Settings page; they shape only what the agents do for that person, so they
need nobody's approval and live in the service's store, not the memory repo. What a
person writes in a conversation is kept as a signal for this alone: it is never
written to a repository's journal. A preference that other people's notes hold too,
and that holds for every review of a repository, is proposed as that repository's
review lesson, which waits for a maintainer like any other.

``PersonNotesScan`` (on each review scan) starts ``LearnPersonNotes`` for a person
with ``LEARN_AFTER`` new signals, at most every ``LEARN_EVERY``; one quick agent run
turns them, with the current notes, into the new notes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
    MemoryChangePayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, Signal, SignalKind, Tier, repo_dir
from ..domain.ports import Store
from ..domain.profile import RepoProfile
from .base import Playbook, PlaybookContext, PlaybookResult
from .prompts import schema_hint, untrusted

NOTES_STATE = "person_notes:{login}"
NOTES_INDEX = "person_notes:index"
"""The logins that have notes, lowercased: the state store lists no keys."""
LEARN_AFTER = 5
"""New signals of a person that start a learning run."""
LEARN_EVERY = timedelta(hours=6)
"""The least time between two learning runs of a person the scan starts."""
MAX_NOTES = 16
NOTE_CHARS = 300
EVIDENCE_CHARS = 30_000
SAID_CHARS = 2_000
REVIEW_KINDS = frozenset({ActionKind.REVIEW_POST.value, ActionKind.REVIEW_UPDATE.value})
LEARNED_FROM = frozenset(
    {
        SignalKind.DRAFT_EDITED,
        SignalKind.PROPOSAL_REJECTED,
        SignalKind.CHAT_INSTRUCTION,
        SignalKind.REVIEW_DISCUSSION,
        SignalKind.CONVERSATION,
    }
)
NOT_JOURNALED = "person_notes"
"""What a person says is consumed by their notes alone (``Signal.consumed_by``), so
the journal never writes it to the memory repo."""
LESSONS_HEADER = "# Review lessons\n"

About = Literal["reviews", "conversations", "both"]


class PersonNote(BaseModel):
    text: str = Field(max_length=NOTE_CHARS)
    about: About = "both"
    source: Literal["learned", "person"] = "learned"
    """``person``: they wrote it, or changed it; learning keeps it as it is."""
    evidence: int = 0
    """How many of the person's messages, edits, and rejections back a learned
    note."""


class PersonNotes(BaseModel):
    login: str
    learning: bool = True
    """Whether what they say and do teaches their notes."""
    notes: list[PersonNote] = Field(default_factory=list)
    learned_to: datetime | None = None
    """The newest signal learning has read."""
    learned_at: datetime | None = None
    """When learning last ran."""
    promoted: list[str] = Field(default_factory=list)
    """Notes already proposed as a repository's lesson."""


async def load_notes(store: Store, login: str) -> PersonNotes:
    raw = await store.get_state(NOTES_STATE.format(login=login.lower()))
    if isinstance(raw, dict):
        try:
            return PersonNotes.model_validate(raw)
        except ValueError:
            pass
    return PersonNotes(login=login)


async def save_notes(store: Store, notes: PersonNotes) -> None:
    key = notes.login.lower()
    await store.set_state(NOTES_STATE.format(login=key), notes.model_dump(mode="json"))
    index = set(await store.get_state(NOTES_INDEX) or [])
    if key not in index:
        await store.set_state(NOTES_INDEX, sorted(index | {key}))


async def add_note(
    store: Store, login: str, text: str, about: About = "both"
) -> PersonNotes:
    """Add a note ``login`` wrote themselves, unless they have it already."""
    notes = await load_notes(store, login)
    text = " ".join(text.split())[:NOTE_CHARS]
    if text and text.lower() not in {n.text.lower() for n in notes.notes}:
        notes.notes.append(PersonNote(text=text, about=about, source="person"))
        await save_notes(store, notes)
    return notes


async def remember_said(
    store: Store,
    login: str,
    text: str,
    *,
    repo: str | None = None,
    number: int | None = None,
    where: str = "chat",
    kind: SignalKind = SignalKind.CONVERSATION,
    action_kind: str | None = None,
    message_id: str | None = None,
    at: datetime | None = None,
) -> None:
    """Keep what ``login`` wrote in a conversation (``where``) for their notes. It
    is kept for that alone: never written to a repository's journal. With
    ``message_id``, the message is kept once, however often it is offered."""
    if not text.strip():
        return
    signal = Signal(
        kind=kind,
        repo=repo or "",
        number=number,
        source=login,
        payload={
            "text": text[:SAID_CHARS],
            "where": where,
            **({"action_kind": action_kind} if action_kind else {}),
        },
        consumed_by=NOT_JOURNALED,
        **({"id": f"sig_said_{message_id}"} if message_id else {}),
        **({"created_at": at} if at else {}),
    )
    try:
        await store.add_signal(signal)
    except Exception:
        if message_id is None:
            raise  # an id of its own cannot clash; a kept message's can


BACKFILLED = "person_notes:backfilled"


async def backfill_said(store: Store) -> int:
    """Once: keep what people wrote in conversations before their notes learned
    from conversations, so their history teaches them too. Returns how many."""
    if await store.get_state(BACKFILLED):
        return 0
    count = 0
    for login, thread, message in await store.list_said():
        where = (
            "review discussion"
            if thread is not None and thread.job_id
            else "People conversation"
            if thread is not None and thread.topic
            else "chat"
        )
        await remember_said(
            store,
            login,
            message.text,
            repo=thread.repo if thread else None,
            number=thread.number if thread else None,
            where=where,
            kind=SignalKind.REVIEW_DISCUSSION
            if where == "review discussion"
            else SignalKind.CONVERSATION,
            message_id=message.id,
            at=message.created_at,
        )
        count += 1
    await store.set_state(BACKFILLED, True)
    return count


def notes_block(notes: PersonNotes, about: Literal["reviews", "conversations"]) -> str:
    """The person's notes ``about`` reviews or conversations, for an agent working
    for them; empty without any."""
    wanted = [n for n in notes.notes if n.about in (about, "both")]
    if not wanted:
        return ""
    what = (
        "the reviews written for them"
        if about == "reviews"
        else "how you talk with them"
    )
    return (
        f"What {notes.login} wants for {what} (their own notes, kept from what they "
        "say and do with the service; the repository's lessons win where the two "
        "differ):\n"
        + untrusted("person.notes", "\n".join(f"- {n.text}" for n in wanted), 6000)
    )


def teaches(signal: Signal) -> bool:
    """Whether ``signal`` says something about what a person wants."""
    if signal.kind not in LEARNED_FROM:
        return False
    if signal.kind in (SignalKind.DRAFT_EDITED, SignalKind.PROPOSAL_REJECTED):
        return str(signal.payload.get("action_kind")) in REVIEW_KINDS
    return True


async def new_signals(store: Store, notes: PersonNotes) -> list[Signal]:
    """The person's signals learning has not read, oldest first."""
    me = notes.login.lower()
    return sorted(
        (
            s
            for s in await store.list_signals(unconsumed=False)
            if s.source.lower() == me
            and teaches(s)
            and (notes.learned_to is None or s.created_at > notes.learned_to)
        ),
        key=lambda s: s.created_at,
    )


def _evidence(signal: Signal) -> str:
    where = f"{signal.repo}#{signal.number}" if signal.number else signal.repo
    on = f" on {where}" if where else ""
    p = signal.payload
    if signal.kind == SignalKind.DRAFT_EDITED:
        return f"Edited a {p.get('action_kind')} draft{on} before posting it, as this diff of the draft shows:\n{str(p.get('diff') or '')[:4000]}"
    if signal.kind == SignalKind.PROPOSAL_REJECTED:
        return f"Turned down a {p.get('action_kind')} draft{on}; their reason: {p.get('reason') or '(none given)'}"
    if signal.kind == SignalKind.CHAT_INSTRUCTION:
        return f"Asked the service to remember{on}: {p.get('lesson')}"
    if signal.kind == SignalKind.REVIEW_DISCUSSION:
        return f"Said discussing a review draft{on}: {str(p.get('text') or '')[:SAID_CHARS]}"
    return f"Said in a conversation ({p.get('where') or 'chat'}){on}: {str(p.get('text') or '')[:SAID_CHARS]}"


class LearnedNote(BaseModel):
    text: str = Field(description="One preference, in one sentence.")
    about: About = Field(
        description="Whether it is about the reviews drafted for them, how the "
        "agents talk with them, or both."
    )
    evidence: int = Field(
        description="How many of the evidence items and the old note's support back it."
    )


class NotesAnswer(BaseModel):
    notes: list[LearnedNote] = Field(
        default_factory=list,
        description=f"Their learned notes as they should be now, at most {MAX_NOTES}.",
    )
    shared: list[str] = Field(
        default_factory=list,
        description="Notes of theirs about reviews that other people hold too and that "
        "hold for every review of the repository, not as one person's taste.",
    )


LEARN = """Keep the notes on what {login} wants from an automated service that helps maintainers run repositories: about the code reviews its agents draft for them (what to look for and leave out, how deep to go, how long, in what tone and language, how to place and word findings), and about how its agents talk with them in conversations (how long and how detailed answers are, what language, what they want shown or checked first, how to propose things). The evidence below is what they said and did since the notes were last updated: their messages in conversations, their edits to review drafts before posting (diffs of the draft's data), what they turned down and why, and what they asked to remember.

- Keep a note only for a habit the evidence shows more than once, or a preference they state; a question about one PR, one remark, or one edit is not a preference. A note says what to do, in one sentence, never names a PR, and holds for anything like it.
- Say what each note is about: `reviews`, `conversations`, or `both`.
- Update the learned notes below: keep those the evidence does not contradict (their support stands), merge duplicates, change those it contradicts, and add new ones. Return all of them, at most {limit}, strongest first, each with how much evidence backs it.
- Do not repeat or contradict their own notes (they wrote those) or the repositories' review lessons.
- Write in the language they write in.
- In `shared`, list notes of theirs about reviews that other people's notes below also hold and that state a rule for every review of the repository, rather than one person's taste. Usually there are none."""


class LearnPersonNotes(Playbook):
    """Update one person's learned notes from their new signals."""

    name = "person_notes"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "quality"
    needs_profile = False

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.payload.get("command") == "person_notes" and bool(
            event.payload.get("by")
        )

    def key(self, event: Event) -> str:
        by = str(event.payload.get("by")).lower()
        return f"person_notes:{by}:{event.payload.get('since') or event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        login = str(event.payload["by"])
        ctx.requested_by = login
        notes = await load_notes(ctx.store, login)
        if not notes.learning:
            return PlaybookResult(result={"skipped": "learning is off"})
        signals = await new_signals(ctx.store, notes)
        if not signals:
            return PlaybookResult(result={"skipped": "nothing new"})
        evidence, size = [], 0
        for signal in reversed(signals):  # the newest first, up to the limit
            text = _evidence(signal)
            if size + len(text) > EVIDENCE_CHARS:
                break
            evidence.insert(0, text)
            size += len(text)
        repos = sorted({s.repo for s in signals if s.repo})
        lessons = "\n\n".join(
            filter(None, [await _lessons(ctx, repo) for repo in repos])
        )
        others = []
        for other in await ctx.store.get_state(NOTES_INDEX) or []:
            if other != login.lower():
                theirs = await load_notes(ctx.store, other)
                others += [n.text for n in theirs.notes if n.about != "conversations"]
        own = [n for n in notes.notes if n.source == "person"]
        learned = [n for n in notes.notes if n.source == "learned"]
        prompt = "\n\n".join(
            part
            for part in [
                LEARN.format(login=login, limit=max(1, MAX_NOTES - len(own))),
                "Their learned notes now:\n"
                + untrusted(
                    "notes.learned",
                    "\n".join(
                        f"- [{n.about}] {n.text} (support {n.evidence})"
                        for n in learned
                    )
                    or "(none yet)",
                    6000,
                ),
                "Their own notes:\n"
                + untrusted(
                    "notes.own",
                    "\n".join(f"- [{n.about}] {n.text}" for n in own),
                    4000,
                )
                if own
                else "",
                "The repositories' review lessons:\n"
                + untrusted("lessons", lessons, 8000)
                if lessons
                else "",
                "Other people's notes about reviews:\n"
                + untrusted("notes.others", "\n".join(f"- {t}" for t in others), 6000)
                if others
                else "",
                "\n\n".join(
                    untrusted(f"evidence.{i}", text, 6000)
                    for i, text in enumerate(evidence, 1)
                ),
                schema_hint(NotesAnswer.model_json_schema()),
            ]
            if part
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.LEARN,
                prompt=prompt,
                workdir=ctx.scratch(),
                mode="read_only",
                budget="quick",
                output_schema=NotesAnswer.model_json_schema(),
                activity="person_notes",
            ),
            NotesAnswer,
        )
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"the notes agent failed: {answer.error}")
        found = NotesAnswer.model_validate(answer.structured)
        mine = {n.text.lower() for n in own}
        kept = [
            PersonNote(
                text=n.text.strip()[:NOTE_CHARS],
                about=n.about,
                evidence=max(0, n.evidence),
            )
            for n in found.notes
            if n.text.strip() and n.text.strip().lower() not in mine
        ][: max(0, MAX_NOTES - len(own))]
        # The person may have changed their notes meanwhile: keep theirs as they are.
        latest = await load_notes(ctx.store, login)
        latest.notes = [n for n in latest.notes if n.source == "person"] + kept
        latest.learned_to = signals[-1].created_at
        latest.learned_at = ctx.clock.now()
        actions = []
        for text in found.shared:
            text = text.strip()
            if not text or text in latest.promoted:
                continue
            latest.promoted.append(text)
            for repo in repos:
                actions.append(await _propose_lesson(ctx, repo, text, login))
        await save_notes(ctx.store, latest)
        return PlaybookResult(
            actions=actions,
            result={
                "notes": len(latest.notes),
                "signals": len(signals),
                "shared": len(found.shared),
            },
        )


async def _lessons(ctx: PlaybookContext, repo: str) -> str:
    profile = await ctx.profiles.active(repo)
    return await ctx.knowledge.review_lessons(repo, profile) if profile else ""


async def _propose_lesson(
    ctx: PlaybookContext, repo: str, lesson: str, login: str
) -> Action:
    """``lesson`` as a change to ``repo``'s review lessons that waits for one of its
    maintainers: the agent wrote it, so it carries nobody's approval."""
    path = f"{repo_dir(repo)}/knowledge/review_lessons.md"
    current = await ctx.memory.read_text(path) or LESSONS_HEADER
    change = MemoryChange(
        tier=Tier.T1,
        title="lesson several reviewers hold",
        description=f"{lesson}\n\nLearned from what {login} and other reviewers "
        "say and do with their review drafts.",
        files={path: current.rstrip() + f"\n- {lesson}\n"},
        repos=[repo],
        created_at=ctx.clock.now(),
    )
    await ctx.store.save_memory_change(change)
    return make_action(
        ActionKind.MEMORY_CHANGE,
        repo,
        MemoryChangePayload(change_id=change.id),
        discriminator=change.id,
    )


class PersonNotesScan(Playbook):
    """On each review scan, start learning for each person who said and did enough
    since their notes last learned, at most every ``LEARN_EVERY``."""

    name = "person_notes_scan"
    triggers = ()
    tick_names = ("review_scan",)

    def key(self, event: Event) -> str:
        return f"person_notes_scan:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        # Every repository's scan looks at everyone (a conversation may be about no
        # repository); the learning job's key starts one per person and starting point.
        backfilled = await backfill_said(ctx.store)
        now = ctx.clock.now()
        people = {
            s.source
            for s in await ctx.store.list_signals(unconsumed=False)
            if s.source and s.source != "service" and teaches(s)
        }
        events = []
        for login in sorted(people):
            notes = await load_notes(ctx.store, login)
            if not notes.learning:
                continue
            if notes.learned_at is not None and now - notes.learned_at < LEARN_EVERY:
                continue
            if len(await new_signals(ctx.store, notes)) < LEARN_AFTER:
                continue
            events.append(learn_event(login, notes))
        return PlaybookResult(
            events=events, result={"learning": len(events), "backfilled": backfilled}
        )


def learn_event(login: str, notes: PersonNotes) -> Event:
    """The request that updates ``login``'s notes from their signals since
    ``notes.learned_to``: one job per person and starting point."""
    return Event(
        kind=EventKind.DASHBOARD_COMMAND,
        payload={
            "command": "person_notes",
            "by": login,
            "since": notes.learned_to.isoformat() if notes.learned_to else "start",
        },
        source="scan",
    )


async def drafts_progress(store: Store, login: str, now: datetime) -> dict[str, object]:
    """How much of the review drafts written for ``login`` they changed before
    posting, in the last 30 days and the 30 before: drafts they posted, and of
    those, how many they edited first. The share should fall as their notes learn."""
    me = login.lower()
    windows = {
        "recent": (now - timedelta(days=30), now),
        "before": (now - timedelta(days=60), now - timedelta(days=30)),
    }
    posted = dict.fromkeys(windows, 0)
    for record in await store.list_actions(
        kinds=[ActionKind.REVIEW_POST], statuses=[ActionStatus.EXECUTED], limit=5000
    ):
        if (record.action.draft_for or "").lower() != me:
            continue
        for w, (start, end) in windows.items():
            if start <= record.action.created_at < end:
                posted[w] += 1
    edited = dict.fromkeys(windows, 0)
    for signal in await store.list_signals(unconsumed=False):
        if (
            signal.source.lower() != me
            or signal.kind != SignalKind.DRAFT_EDITED
            or signal.payload.get("action_kind") != ActionKind.REVIEW_POST.value
        ):
            continue
        for w, (start, end) in windows.items():
            if start <= signal.created_at < end:
                edited[w] += 1
    return {w: {"posted": posted[w], "edited": edited[w]} for w in windows}
