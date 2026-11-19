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

"""Each person's own notes: learned from everything they say in conversations and do
with their drafts, read only by what the agents do for them, and theirs to change."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import TaskKind
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.memory import Signal, SignalKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.playbooks.person_notes import (
    add_note,
    load_notes,
    save_notes,
)
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


def _tick(name: str = "evt_tick") -> Event:
    return Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"}, id=name)


async def _signals(h: Any, login: str) -> None:
    for signal in (
        Signal(
            kind=SignalKind.DRAFT_EDITED,
            repo=REPO,
            number=42,
            source=login,
            payload={
                "action_kind": "review.post",
                "diff": '-   "title": "Prefer f-strings",\n-   "severity": "nit",',
            },
        ),
        Signal(
            kind=SignalKind.PROPOSAL_REJECTED,
            repo=REPO,
            number=43,
            source=login,
            payload={"action_kind": "review.post", "reason": "only style nits"},
        ),
        Signal(  # a label: nothing about what they want from the agents
            kind=SignalKind.PROPOSAL_REJECTED,
            repo=REPO,
            number=44,
            source=login,
            payload={"action_kind": "label.apply", "reason": "wrong label"},
        ),
    ):
        await h.store.add_signal(
            signal.model_copy(update={"created_at": h.clock.now()})
        )


async def _say(app: Any, login: str, *texts: str) -> None:
    async with _as(app, login) as client:
        thread = (await client.post("/api/chat/threads", json={"text": "hi"})).json()
        for text in texts:
            sent = await client.post(
                f"/api/chat/threads/{thread['id']}/messages", json={"text": text}
            )
            assert sent.status_code == 200, sent.text


async def test_everything_a_person_says_and_does_teaches_their_notes(demo) -> None:
    h, app = demo
    chats: list[Any] = []

    def chat(task: Any) -> dict[str, Any]:
        chats.append(task)
        return {"reply": "ok", "proposals": []}

    def learn(task: Any) -> dict[str, Any]:
        learned.append(task)
        return {
            "notes": [
                {"text": "Leave out style nits.", "about": "reviews", "evidence": 3},
                {
                    "text": "Point out every missing test.",
                    "about": "both",
                    "evidence": 1,
                },
                {
                    "text": "Answer briefly, in Chinese.",
                    "about": "conversations",
                    "evidence": 2,
                },
            ],
            "shared": [{"repo": REPO, "lesson": "Leave out style nits."}],
        }

    learned: list[Any] = []
    h.runner.on("chat", chat)
    h.runner.on("learn", learn)
    await add_note(h.store, "walt", "Write my reviews in English.", "reviews")
    await add_note(h.store, "maria", "Point out every missing test.")
    await _signals(h, "maria")
    await _say(app, "maria", "Why did #42's CI fail? 简短一点")
    await h.emit(_tick())
    await h.settle()
    assert learned == [], "four signals: not enough yet"

    await _say(app, "maria", "用中文回答，简短些。", "Thanks.")
    await h.emit(_tick("evt_tick2"))
    await h.settle()
    [task] = learned
    assert task.budget == "quick" and task.requested_by == "maria"
    assert "简短一点" in task.prompt and "用中文回答，简短些。" in task.prompt
    assert "only style nits" in task.prompt and "wrong label" not in task.prompt
    assert "Write my reviews in English." in task.prompt, "others' notes, to share"
    notes = await load_notes(h.store, "maria")
    assert [(n.text, n.about, n.source) for n in notes.notes] == [
        ("Point out every missing test.", "both", "person"),
        ("Leave out style nits.", "reviews", "learned"),
        ("Answer briefly, in Chinese.", "conversations", "learned"),
    ], "her own note stays hers, once"
    lessons = [
        c
        for c in await h.store.list_memory_changes()
        if c.title == "lesson several reviewers hold"
    ]
    assert len(lessons) == 1 and lessons[0].approvals == {}, "a maintainer decides"
    journaled = [
        s
        for s in await h.store.list_signals(unconsumed=True)
        if s.kind == SignalKind.CONVERSATION
    ]
    assert journaled == [], "what people say never goes to a repository's journal"

    h.clock.advance(hours=7)
    await h.emit(_tick("evt_tick3"))
    await h.settle()
    assert len(learned) == 1, "nothing new since"

    h.runner.on("review", lambda t: {"summary": "s", "findings": []})
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
    async with _as(app, "walt") as walt:
        await walt.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 43}]})
    await h.settle()
    reviews = {
        t.requested_by: t.prompt for t in h.runner.tasks if t.kind == TaskKind.REVIEW
    }
    assert "Leave out style nits." in reviews["maria"]
    assert "Answer briefly, in Chinese." not in reviews["maria"], "about conversations"
    assert "Leave out style nits." not in reviews["walt"]
    assert "Write my reviews in English." in reviews["walt"]
    await _say(app, "maria", "And #43?")
    assert "Answer briefly, in Chinese." in chats[-1].prompt
    assert "Leave out style nits." not in chats[-1].prompt, "about reviews"


async def test_a_person_sees_and_changes_only_their_own_notes(demo) -> None:
    h, app = demo
    notes = await add_note(h.store, "maria", "Point out every missing test.")
    notes.notes[0].source = "learned"
    notes.notes[0].evidence = 4
    await save_notes(h.store, notes)
    await _signals(h, "maria")
    h.clock.advance(minutes=1)
    async with _as(app, "maria") as maria:
        view = (await maria.get("/api/me/notes")).json()
        assert view["new_signals"] == 2 and view["learning"] is True
        assert view["progress"]["recent"]["edited"] == 1
        put = await maria.put(
            "/api/me/notes",
            json={
                "learning": True,
                "notes": [
                    {"text": "Point out every missing test.", "source": "learned"},
                    {"text": "Keep inline comments short.", "source": "learned"},
                    {"text": "Answer briefly.", "about": "conversations"},
                ],
            },
        )
        saved = put.json()["notes"]
        assert [(n["source"], n["evidence"], n["about"]) for n in saved] == [
            ("learned", 4, "both"),
            ("person", 0, "both"),
            ("person", 0, "conversations"),
        ], "a learned note kept word for word stays learned; a new one is hers"
        started = await maria.post("/api/me/notes/learn")
        assert started.status_code == 200
        off = await maria.put("/api/me/notes", json={"learning": False, "notes": []})
        assert off.json()["learning"] is False
        assert (await maria.post("/api/me/notes/learn")).status_code == 409
    async with _as(app, "walt") as walt:
        assert (await walt.get("/api/me/notes")).json()["notes"] == []


async def test_the_chat_saves_a_preference_to_the_persons_notes(demo) -> None:
    h, app = demo
    h.runner.on(
        "chat",
        lambda t: {
            "reply": "Noted.",
            "proposals": [
                {
                    "kind": "preference",
                    "lesson": "Skip style nits for me.",
                    "about": "reviews",
                }
            ],
        },
    )
    async with _as(app, "walt") as walt:
        thread = (await walt.post("/api/chat/threads", json={"text": "hi"})).json()
        sent = await walt.post(
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "Please never give me style nits."},
        )
        assert "your preferences" in sent.json()["text"]
    notes = await load_notes(h.store, "walt")
    assert [(n.text, n.about, n.source) for n in notes.notes] == [
        ("Skip style nits for me.", "reviews", "person")
    ]


async def test_what_people_said_before_is_kept_once(demo) -> None:
    """The first scan keeps the conversations people had before their notes
    learned from conversations, each message once, beside those kept as they
    were sent."""
    from auto_maintainer.domain.records import ChatMessage, ChatThread

    h, app = demo
    thread = ChatThread(owner="maria", repo=REPO, number=42)
    await h.store.save_thread(thread)
    for text in ("Shorter answers, please.", "In Chinese."):
        await h.store.add_message(
            ChatMessage(thread_id=thread.id, role="user", text=text)
        )
    await h.store.add_message(
        ChatMessage(thread_id=thread.id, role="assistant", text="Sure.")
    )
    h.runner.on("chat", lambda t: {"reply": "ok", "proposals": []})
    await _say(app, "maria", "Why did CI fail?")
    for name in ("evt_a", "evt_b"):
        await h.emit(_tick(name))
        await h.settle()
    said = [
        s.payload["text"]
        for s in await h.store.list_signals(unconsumed=False)
        if s.kind == SignalKind.CONVERSATION and s.source == "maria"
    ]
    assert sorted(said) == [
        "In Chinese.",
        "Shorter answers, please.",
        "Why did CI fail?",
    ]


async def test_the_owner_starts_learning_for_everyone_now(demo) -> None:
    h, app = demo
    await _signals(h, "maria")
    await _signals(h, "walt")
    off = await load_notes(h.store, "walt")
    off.learning = False
    await save_notes(h.store, off)
    learned: list[Any] = []
    h.runner.on(
        "learn",
        lambda t: (learned.append(t.requested_by), {"notes": []})[1],
    )
    async with _as(app, "walt") as walt:
        assert (await walt.post("/api/me/notes/everyone/learn")).status_code == 403
    async with _as(app, "maria") as maria:
        out = (await maria.post("/api/me/notes/everyone/learn")).json()
    await h.settle()
    assert out["started"] == ["maria"], "walt turned his learning off"
    assert learned == ["maria"]


async def test_shared_lessons_are_scoped_per_repo_and_deduplicated(demo) -> None:
    """Lessons in the prompt identify their repository (including open pending ones),
    shared lessons apply only to their named repository, duplicates against existing
    or pending lessons are dropped, and multiple lessons for one repo merge into one
    change with consolidation via ``replaces``."""
    from auto_maintainer.domain.memory import (
        MemoryChange,
        MemoryChangeState,
        Tier,
        repo_dir,
    )
    from auto_maintainer.domain.records import RepoRecord
    from auto_maintainer.domain.snapshot import RepoInfo

    h, app = demo
    other = "acme/other"
    h.forge.repos[other] = RepoInfo(name=other)
    h.forge.set_permission(other, "maria", Permission.ADMIN)
    await h.store.save_repo(RepoRecord(repo=other, active=True, added_by="maria"))
    h.memory.files[f"{repo_dir(other)}/profile.yaml"] = "schema: 1\n"
    state = await h.service.profiles.refresh(other)
    assert state.candidate_hash is not None
    _, lock_change = await h.service.profiles.approve_candidate(
        other, "maria", state.candidate_hash
    )
    assert lock_change is not None
    await h.memory.merge_change(lock_change)
    h.service.profiles.invalidate(other)

    widget_path = f"{repo_dir(REPO)}/knowledge/review_lessons.md"
    other_path = f"{repo_dir(other)}/knowledge/review_lessons.md"
    h.memory.files[widget_path] = (
        "# Review lessons\n"
        "- Skip minor style nits.\n"
        "- Do not leave style nit comments.\n"
        "- Require tests for bug fixes.\n"
    )
    h.memory.files[other_path] = "# Review lessons\n- Check API migrations.\n"
    # An already-open pending change for acme/widget that should be shown & deduplicated.
    await h.store.save_memory_change(
        MemoryChange(
            tier=Tier.T1,
            title="pending widget lesson",
            files={
                widget_path: h.memory.files[widget_path]
                + "- Always check lock ordering.\n"
            },
            repos=[REPO],
        )
    )

    for i in range(5):
        await h.store.add_signal(
            Signal(
                kind=SignalKind.CONVERSATION,
                repo=REPO if i < 3 else other,
                source="maria",
                payload={"text": f"signal {i}", "where": "chat"},
                created_at=h.clock.now(),
            )
        )
    await add_note(h.store, "walt", "Skip style nits.", "reviews")

    seen_prompts: list[str] = []

    def learn(task: Any) -> dict[str, Any]:
        seen_prompts.append(task.prompt)
        return {
            "notes": [{"text": "Skip style nits.", "about": "reviews", "evidence": 3}],
            "shared": [
                # Duplicate of existing widget lesson (different case/punctuation).
                {"repo": REPO, "lesson": "require tests for bug fixes"},
                # Duplicate of an open pending widget lesson.
                {"repo": REPO, "lesson": "Always check lock ordering."},
                # Consolidates two redundant widget bullets into one.
                {
                    "repo": REPO,
                    "lesson": "Leave out minor style nits in reviews.",
                    "replaces": [
                        "Skip minor style nits.",
                        "Do not leave style nit comments.",
                    ],
                },
                # A second genuinely new widget lesson in the same run -> same change.
                {"repo": REPO, "lesson": "Flag unclosed file handles."},
                # Unknown repo -> ignored.
                {"repo": "acme/unknown", "lesson": "Should be ignored."},
            ],
        }

    h.runner.on("learn", learn)
    await h.emit(_tick("evt_multi"))
    await h.settle()

    [prompt] = seen_prompts
    assert f"## {REPO}" in prompt and f"## {other}" in prompt
    assert "Check API migrations." in prompt
    assert "Always check lock ordering." in prompt, "open pending lesson shown"

    proposed = [
        c
        for c in await h.store.list_memory_changes(MemoryChangeState.OPEN)
        if c.title.endswith("several reviewers hold")
    ]
    assert len(proposed) == 1, "one consolidated change for acme/widget, none for other"
    change = proposed[0]
    assert change.repos == [REPO]
    text = change.files[widget_path]
    assert text is not None
    assert "- Leave out minor style nits in reviews." in text
    assert "- Flag unclosed file handles." in text
    assert "- Require tests for bug fixes." in text
    assert "Skip minor style nits." not in text
    assert "Do not leave style nit comments." not in text
    assert "Always check lock ordering." not in text

    # A second run proposing the same lesson for acme/widget is skipped via promoted.
    h.clock.advance(hours=7)
    for i in range(5):
        await h.store.add_signal(
            Signal(
                kind=SignalKind.CONVERSATION,
                repo=REPO,
                source="maria",
                payload={"text": f"more {i}", "where": "chat"},
                created_at=h.clock.now(),
            )
        )
    h.runner.on(
        "learn",
        lambda t: {
            "notes": [],
            "shared": [{"repo": REPO, "lesson": "flag unclosed file handles"}],
        },
    )
    await h.emit(_tick("evt_multi2"))
    await h.settle()
    after = [
        c
        for c in await h.store.list_memory_changes(MemoryChangeState.OPEN)
        if c.title.endswith("several reviewers hold")
    ]
    assert len(after) == 1, "normalized promoted key prevents duplicate re-proposal"
