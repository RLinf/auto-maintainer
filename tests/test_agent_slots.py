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

"""Agent slots: a job is claimed only when it starts, a person's request never waits
behind the service's own work, the number of slots changes live, and a queued job says
what it waits for."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.records import Job, JobStatus
from auto_maintainer.playbooks.base import Playbook, PlaybookContext, PlaybookResult
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app
from auto_maintainer.web.jobs import slot_view


class Hold(Playbook):
    """Runs until the test lets it end."""

    name = "hold"
    triggers: ClassVar = ()
    pool = "agent"
    needs_profile = False

    def __init__(self) -> None:
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        name = str(event.payload["name"])
        self.started.append(name)
        await self.gates.setdefault(name, asyncio.Event()).wait()
        return PlaybookResult()

    def end(self, name: str) -> None:
        self.gates.setdefault(name, asyncio.Event()).set()


@pytest.fixture
async def h(tmp_path: Path):
    harness = await Harness.create(tmp_path)
    yield harness
    await harness.close()


async def _queue(h: Harness, name: str, by: str | None = None) -> Job:
    """A job of ``Hold``: a person's request when ``by`` is set, else the service's own."""
    payload: dict[str, Any] = {"name": name}
    if by:
        payload["by"] = by
    job = Job(
        playbook="hold",
        key=f"hold:{name}",
        pool="agent",
        events=[
            Event(
                kind=EventKind.DASHBOARD_COMMAND if by else EventKind.SCHEDULE_TICK,
                payload=payload,
            ).model_dump(mode="json")
        ],
    )
    job.created_at = job.updated_at = h.clock.now()
    h.clock.advance(seconds=1)
    job, _ = await h.store.upsert_job(job)
    h.service.engine.resize("agent", h.service.engine.pools["agent"])  # wakes the pool
    return job


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the engine did not get there: {check.__doc__ or ''}")


async def _status(h: Harness, job: Job) -> JobStatus:
    current = await h.store.get_job(job.id)
    assert current is not None
    return current.status


async def test_people_go_first_and_jobs_are_claimed_only_when_they_start(
    h: Harness,
) -> None:
    hold = Hold()
    engine = h.service.engine
    engine.playbooks["hold"] = hold
    engine.resize("agent", 2)
    engine.start()
    try:
        background = [await _queue(h, f"bg{i}") for i in range(3)]
        await _until(lambda: hold.started == ["bg0", "bg1"])
        await asyncio.sleep(0.05)
        assert await _status(h, background[2]) == JobStatus.QUEUED, (
            "a job waiting for a slot is not shown as running"
        )

        # Both slots hold the service's own work; a person's request starts anyway.
        first = await _queue(h, "walt-1", by="walt")
        await _until(lambda: "walt-1" in hold.started)
        assert engine.slots("agent") == {
            "size": 2,
            "people": 1,
            "service": 2,
            "free": 1,
        }
        await _queue(h, "walt-2", by="walt")
        await _until(lambda: "walt-2" in hold.started)
        third = await _queue(h, "maria-1", by="maria")
        await asyncio.sleep(0.05)
        assert await _status(h, third) == JobStatus.QUEUED, (
            "people's requests wait for each other"
        )
        slot = await slot_view(h.service, third)
        assert slot == {"size": 2, "people": 2, "service": 2, "free": 0, "ahead": 0}
        waiting = await slot_view(h.service, background[2])
        assert waiting is not None and waiting["ahead"] == 1, (
            "maria's request goes first"
        )
        running = await h.store.get_job(first.id)
        assert running is not None
        assert await slot_view(h.service, running) is None, (
            "a running job waits for nothing"
        )

        hold.end("walt-1")
        await _until(lambda: "maria-1" in hold.started)
        hold.end("bg0")
        hold.end("bg1")
        await asyncio.sleep(0.05)
        assert "bg2" not in hold.started, (
            "the service's own work waits while people's requests fill the slots"
        )

        engine.resize("agent", 3)  # the owner raises the slots: it applies at once
        await _until(lambda: "bg2" in hold.started)
    finally:
        for name in list(hold.gates) + ["bg2", "walt-2", "maria-1"]:
            hold.end(name)
        await engine.stop()


async def test_the_settings_set_the_slots_live(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    try:
        await _set_slots_live(h, app)
    finally:
        await h.close()


async def _set_slots_live(h: Harness, app: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as maria:
        await maria.get("/auth/dev?login=maria")
        maria.headers["x-am-csrf"] = maria.cookies.get("am_csrf") or ""
        before = (await maria.get("/api/settings")).json()["system"]
        assert (
            before["agent_slots"] == 3 and "agent_slots" not in before["from_settings"]
        )
        saved = await maria.put("/api/settings/agent-slots", json={"slots": 6})
        assert saved.status_code == 200, saved.text
        after = (await maria.get("/api/settings")).json()["system"]
        assert after["agent_slots"] == 6 and "agent_slots" in after["from_settings"]
        assert after["agent_slots_in_use"] == {"people": 0, "service": 0}
        assert (
            await maria.put("/api/settings/agent-slots", json={"slots": 0})
        ).status_code == 422
        await maria.put("/api/settings/agent-slots", json={"slots": None})
        reset = (await maria.get("/api/settings")).json()["system"]
    assert h.service.engine.pools["agent"] == 3 and reset["agent_slots"] == 3
