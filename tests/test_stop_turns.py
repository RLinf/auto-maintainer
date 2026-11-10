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

from auto_maintainer.domain.agents import AgentTask
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


async def test_a_chat_turn_stops_and_a_new_message_interrupts(demo) -> None:
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
        turn = asyncio.create_task(maria.post(f"{base}/messages", json={"text": "one"}))
        await asyncio.wait_for(started.wait(), 5)
        stopped = await maria.post(f"{base}/stop")
        assert stopped.json() == {"stopped": True}
        answer = (await asyncio.wait_for(turn, 10)).json()
        assert answer["text"] == "Stopped by maria." and ended == ["cancelled"]

        started.clear()
        first = asyncio.create_task(
            maria.post(f"{base}/messages", json={"text": "two"})
        )
        await asyncio.wait_for(started.wait(), 5)
        h.runner.on("chat", lambda task: {"reply": "the answer to three"})
        second = await maria.post(f"{base}/messages", json={"text": "three"})
        assert (await asyncio.wait_for(first, 10)).json()["text"] == "Stopped by maria."
        assert second.status_code == 200, second.text
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
