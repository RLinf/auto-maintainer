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

"""Agents' questions on the dashboard; dev mode's sandbox repository and sign-in."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from auto_maintainer.domain.actions import ActionKind, LabelPayload, make_action
from auto_maintainer.domain.agents import (
    AgentAnswer,
    AgentQuestion,
    InputQuestion,
    QuestionKind,
    QuestionOption,
    QuestionState,
)
from auto_maintainer.domain.events import EventKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.engine.questions import QuestionDesk
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


def _question(**kw: object) -> AgentQuestion:
    data: dict[str, object] = {
        "kind": QuestionKind.PERMISSION,
        "title": "run: curl https://x.test",
        "detail": "curl https://x.test",
        "tool": "execute",
        "repo": REPO,
        "pr": 42,
        "runtime": "antigravity",
        "options": [
            QuestionOption(id="allow", label="Allow", kind="allow"),
            QuestionOption(id="deny", label="Deny", kind="deny"),
        ],
    }
    data.update(kw)
    return AgentQuestion.model_validate(data)


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    h.service.questions.poll_seconds = 0.05
    yield h
    await h.close()


@contextlib.asynccontextmanager
async def _client(app, login: str | None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        if login:
            await client.get(f"/auth/dev?login={login}")
            client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def _pending(desk: QuestionDesk, store) -> AgentQuestion:
    for _ in range(100):
        rows = await store.list_questions(QuestionState.PENDING)
        if rows:
            return rows[0]
        await asyncio.sleep(0.02)
    raise AssertionError("no pending question")


async def test_a_maintainer_answers_on_the_dashboard(demo, tmp_path: Path) -> None:
    h = demo
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    events: list = []
    h.service.bus.subscribe(lambda e: _record(events, e))
    waiting = asyncio.create_task(h.service.questions.ask(_question(), 60))
    question = await _pending(h.service.questions, h.store)
    async with _client(app, "stranger") as stranger:
        assert (await stranger.get("/api/questions")).json() == []
        denied = await stranger.post(
            f"/api/questions/{question.id}/answer", json={"allow": True}
        )
        assert denied.status_code == 403
    async with _client(app, "walt") as walt:
        assert (await walt.get("/api/me")).json()["pending_questions"] == 1
        [row] = (await walt.get("/api/questions")).json()
        assert row["detail"] == "curl https://x.test" and row["pr"] == 42
        out = await walt.post(
            f"/api/questions/{question.id}/answer",
            json={"allow": False, "message": "no network in reviews"},
        )
        assert out.status_code == 200 and out.json()["state"] == "answered"
        again = await walt.post(
            f"/api/questions/{question.id}/answer", json={"allow": True}
        )
        assert again.status_code == 409
    answer = await asyncio.wait_for(waiting, 5)
    assert not answer.allow and answer.option_id == "deny" and answer.by == "walt"
    assert answer.message == "no network in reviews"
    await h.service.bus.drain()
    kinds = [e.kind for e in events]
    assert EventKind.QUESTION_ASKED in kinds and EventKind.QUESTION_ANSWERED in kinds
    audit = await h.store.list_audit(REPO)
    assert any(a.kind == "agent_question" and a.actor == "walt" for a in audit)


async def _record(events: list, event) -> None:
    events.append(event)


async def test_an_answer_from_another_process_is_seen(demo) -> None:
    h = demo
    desk = h.service.questions
    waiting = asyncio.create_task(desk.ask(_question(), 60))
    question = await _pending(desk, h.store)
    other = QuestionDesk(h.store, h.service.bus, h.service.a.notifier, h.clock)
    await other.answer(question.id, AgentAnswer(allow=True), "walt")
    answer = await asyncio.wait_for(waiting, 5)
    assert answer.allow and answer.option_id == "allow"


async def test_unanswered_questions_are_denied(demo) -> None:
    h = demo
    answer = await h.service.questions.ask(_question(), 0.2)
    assert (
        not answer.allow
        and answer.automatic
        and "No reviewer answered" in answer.message
    )
    [row] = await h.store.list_questions(QuestionState.EXPIRED)
    assert row.answer is not None and not row.answer.allow


async def test_answers_must_fit_the_question(demo) -> None:
    h = demo
    desk = h.service.questions
    asking = asyncio.create_task(
        desk.ask(
            _question(
                kind=QuestionKind.INPUT,
                options=[],
                inputs=[
                    InputQuestion(
                        id="scope", question="Docs too?", options=["Yes", "No"]
                    )
                ],
            ),
            60,
        )
    )
    question = await _pending(desk, h.store)
    with pytest.raises(ValueError, match="missing"):
        await desk.answer(question.id, AgentAnswer(allow=True), "walt")
    await desk.answer(
        question.id, AgentAnswer(allow=True, answers={"scope": ["Yes"]}), "walt"
    )
    assert (await asyncio.wait_for(asking, 5)).answers == {"scope": ["Yes"]}
    with pytest.raises(LookupError):
        await desk.answer("q_missing", AgentAnswer(allow=True), "walt")


async def test_a_cancelled_run_cancels_its_question_and_orphans_are_closed(
    demo,
) -> None:
    h = demo
    desk = h.service.questions
    task = asyncio.create_task(desk.ask(_question(), 60))
    question = await _pending(desk, h.store)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert (await h.store.get_question(question.id)).state == QuestionState.CANCELLED

    import socket

    orphan = _question(waiter=f"{socket.gethostname()}:999999999")
    await h.store.save_question(orphan)
    assert await desk.cancel_orphans() == 1
    assert (await h.store.get_question(orphan.id)).state == QuestionState.CANCELLED


async def test_questions_outside_a_repository_are_for_operators(
    demo, tmp_path: Path
) -> None:
    h = demo
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    question = _question(repo=None, pr=None)
    await h.store.save_question(question)
    async with _client(app, "walt") as walt:
        assert (await walt.get("/api/questions")).json() == []
    h.service.settings.operators.add("olga")
    async with _client(app, "olga") as olga:
        assert [q["id"] for q in (await olga.get("/api/questions")).json()] == [
            question.id
        ]


# -- item 2 and 3: sign-in and the sandbox repository ---------------------------------


async def test_dev_login_exists_only_when_asked_for(demo, tmp_path: Path) -> None:
    h = demo
    h.service.set_dev_mode(True)
    app = create_app(h.service, data_dir=tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await c.get("/auth/dev?login=maria")
        assert (await c.get("/api/me")).status_code == 401
        assert (await c.get("/auth/config")).json()["dev_login"] is False


async def test_a_dev_instance_serves_only_its_sandbox(demo) -> None:
    h = demo
    service = h.service
    service.set_dev_mode(True)
    service.settings.sandbox_repo = "acme/sandbox"
    service.pipeline.sandbox_repo = "acme/sandbox"
    assert service.serves("acme/sandbox") and not service.serves(REPO)
    with pytest.raises(ValueError, match="sandbox"):
        await service.add_repo(REPO, "maria")
    assert service.pipeline.outside_sandbox(REPO)
    assert not service.pipeline.capped(
        "acme/sandbox"
    ) and not service.pipeline.outside_sandbox("acme/sandbox")
    action = make_action(
        ActionKind.LABEL_APPLY, REPO, LabelPayload(labels=["x"]), number=42
    )
    record = await service.pipeline.submit(action)
    assert record.decision is not None and record.decision.rule == "dev_mode.sandbox"

    service.settings.sandbox_repo = None
    service.pipeline.sandbox_repo = None
    assert service.serves(REPO) and service.pipeline.capped(REPO)
