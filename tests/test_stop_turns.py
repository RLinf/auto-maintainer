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

"""A person stops the agent answering in a conversation, or interrupts it with a
new message, and the conversation goes on at once."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import AgentResult, AgentTask
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_chat_turn_returns_immediately_stops_and_interrupts(demo) -> None:
    h, app = demo
    started = asyncio.Event()
    ended: list[str] = []

    async def slow(task: AgentTask) -> dict:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise
        return {"reply": "late"}

    h.runner.on("chat", slow)
    async with _as(app, "maria") as maria:
        thread = (
            await maria.post("/api/chat/threads", json={"text": "hi", "context": {}})
        ).json()
        base = f"/api/chat/threads/{thread['id']}"
        first_post = await maria.post(
            f"{base}/messages", json={"text": "one", "client_message_id": "m1"}
        )
        assert first_post.status_code == 200, first_post.text
        body = first_post.json()
        assert body["live"] is True and body["deduplicated"] is False
        assert body["message"]["text"] == "one"
        assert body["message"]["client_message_id"] == "m1"
        await asyncio.wait_for(started.wait(), 5)
        steps = (await maria.get(f"{base}/steps")).json()
        assert steps["live"] is True and steps["run"] == body["run"]

        stopped = await maria.post(f"{base}/stop")
        assert stopped.json() == {"stopped": True}
        assert ended == ["cancelled"]
        assert (await maria.get(f"{base}/steps")).json()["live"] is False
        msgs = (await maria.get(f"{base}/messages")).json()
        assert [m["text"] for m in msgs] == ["one", "Stopped by maria."]

        started.clear()
        second_post = await maria.post(
            f"{base}/messages", json={"text": "two", "client_message_id": "m2"}
        )
        assert second_post.status_code == 200
        await asyncio.wait_for(started.wait(), 5)
        h.runner.on("chat", lambda task: {"reply": "the answer to three"})
        third_post = await maria.post(
            f"{base}/messages", json={"text": "three", "client_message_id": "m3"}
        )
        assert third_post.status_code == 200, third_post.text
        await app.state.live_turns.wait(third_post.json()["run"], timeout=5)
        assert (await maria.get(f"{base}/steps")).json()["live"] is False
        texts = [m["text"] for m in (await maria.get(f"{base}/messages")).json()]
        assert texts[-4:] == [
            "two",
            "Stopped by maria.",
            "three",
            "the answer to three",
        ]
        assert (await maria.post(f"{base}/stop")).json() == {"stopped": False}
    async with _as(app, "walt") as walt:
        assert (await walt.post(f"{base}/stop")).status_code == 404, "not his"


async def test_replayed_chat_post_deduplicates_without_stopping_or_rerunning(
    demo,
) -> None:
    """Replaying a POST with the same client_message_id while a turn is running or
    after it completes does not duplicate the message, stop the turn, or rerun it."""
    h, app = demo
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_once(task: AgentTask) -> dict:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"reply": f"answer {calls}"}

    h.runner.on("chat", slow_once)
    async with _as(app, "maria") as maria:
        thread = (
            await maria.post("/api/chat/threads", json={"text": "hi", "context": {}})
        ).json()
        base = f"/api/chat/threads/{thread['id']}"

        # Concurrent duplicate deliveries race into the same endpoint.
        r1, r2 = await asyncio.gather(
            maria.post(
                f"{base}/messages",
                json={"text": "why twice?", "client_message_id": "idem-1"},
            ),
            maria.post(
                f"{base}/messages",
                json={"text": "why twice?", "client_message_id": "idem-1"},
            ),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        b1, b2 = r1.json(), r2.json()
        assert {b1["deduplicated"], b2["deduplicated"]} == {False, True}
        assert b1["message"]["id"] == b2["message"]["id"]
        assert b1["run"] == b2["run"]
        await asyncio.wait_for(started.wait(), 5)

        # Replay again while the turn is actively running: must NOT stop it.
        r3 = await maria.post(
            f"{base}/messages",
            json={"text": "why twice?", "client_message_id": "idem-1"},
        )
        assert r3.status_code == 200
        b3 = r3.json()
        assert b3["deduplicated"] is True and b3["live"] is True
        assert b3["run"] == b1["run"]
        assert b3["message"]["id"] == b1["message"]["id"]

        # Let the single turn finish.
        release.set()
        await app.state.live_turns.wait(b1["run"], timeout=5)
        assert calls == 1
        msgs = (await maria.get(f"{base}/messages")).json()
        assert [(m["role"], m["text"]) for m in msgs] == [
            ("user", "why twice?"),
            ("assistant", "answer 1"),
        ]

        # Replay after completion: must NOT re-run or duplicate.
        r4 = await maria.post(
            f"{base}/messages",
            json={"text": "why twice?", "client_message_id": "idem-1"},
        )
        assert r4.status_code == 200
        b4 = r4.json()
        assert b4["deduplicated"] is True and b4["live"] is False
        assert b4["reply"]["text"] == "answer 1"
        assert calls == 1

        # A stale replay of idem-1 while a newer message is running must NOT stop
        # the newer turn either.
        started.clear()
        release.clear()
        newer = (
            await maria.post(
                f"{base}/messages",
                json={"text": "second question", "client_message_id": "idem-2"},
            )
        ).json()
        assert newer["live"] is True and newer["deduplicated"] is False
        await asyncio.wait_for(started.wait(), 5)

        stale = (
            await maria.post(
                f"{base}/messages",
                json={"text": "why twice?", "client_message_id": "idem-1"},
            )
        ).json()
        assert stale["deduplicated"] is True and stale["live"] is False
        assert stale["reply"]["text"] == "answer 1"
        assert app.state.live_turns.running(newer["run"]) is True

        release.set()
        await app.state.live_turns.wait(newer["run"], timeout=5)
        assert calls == 2
        msgs = (await maria.get(f"{base}/messages")).json()
        assert [(m["role"], m["text"]) for m in msgs] == [
            ("user", "why twice?"),
            ("assistant", "answer 1"),
            ("user", "second question"),
            ("assistant", "answer 2"),
        ]


async def test_failed_background_chat_turn_surfaces_in_messages_and_clears_live(
    demo,
) -> None:
    """When the agent fails in the background, /steps stops being live and /messages
    records an error message without poisoning the next turn's history."""
    h, app = demo
    seen_prompts: list[str] = []

    def fail_then_ok(task: AgentTask) -> AgentResult | dict:
        seen_prompts.append(task.prompt)
        if len(seen_prompts) == 1:
            return AgentResult(
                runtime="fake",
                ok=False,
                error="daily token limit reached",
                error_kind="limit",
            )
        return {"reply": "recovered", "proposals": []}

    h.runner.on("chat", fail_then_ok)
    async with _as(app, "maria") as maria:
        thread = (
            await maria.post("/api/chat/threads", json={"text": "hi", "context": {}})
        ).json()
        base = f"/api/chat/threads/{thread['id']}"
        sent = (
            await maria.post(
                f"{base}/messages",
                json={"text": "first", "client_message_id": "err-1"},
            )
        ).json()
        await app.state.live_turns.wait(sent["run"], timeout=5)

        assert (await maria.get(f"{base}/steps")).json()["live"] is False
        msgs = (await maria.get(f"{base}/messages")).json()
        assert len(msgs) == 2
        assert msgs[1]["role"] == "assistant" and msgs[1]["error"] is True
        assert "daily token limit reached" in msgs[1]["text"]
        assert msgs[1]["run_id"] == sent["run"]

        sent2 = (
            await maria.post(
                f"{base}/messages",
                json={"text": "second", "client_message_id": "err-2"},
            )
        ).json()
        await app.state.live_turns.wait(sent2["run"], timeout=5)
        msgs = (await maria.get(f"{base}/messages")).json()
        assert msgs[-1]["text"] == "recovered" and msgs[-1]["error"] is False
    assert "daily token limit reached" not in seen_prompts[1]
