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

"""What keeps open dashboards current: events reach them without waiting on slow
work, and changes that start no playbook still tell them."""

from __future__ import annotations

import asyncio
from pathlib import Path

from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.snapshot import ReviewThread, ThreadComment
from auto_maintainer.engine.bus import EventBus
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness


async def test_slow_work_after_an_event_holds_back_no_other_event() -> None:
    bus = EventBus()
    slow = asyncio.Event()

    async def handler(event: Event) -> None:
        if event.kind == EventKind.ACTION_EXECUTED:
            bus.later(slow.wait(), "read the forge again")

    bus.subscribe(handler)
    heard: list[str] = []

    async def listen() -> None:
        async for event in bus.stream(heartbeat=0.05):
            if event is not None:
                heard.append(event.kind)
            if len(heard) == 2:
                return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await bus.publish(Event(kind=EventKind.ACTION_EXECUTED, repo="o/r", number=1))
    await bus.publish(Event(kind=EventKind.PR_COMMENT, repo="o/r", number=2))
    await asyncio.wait_for(listener, 1)
    assert heard == ["action.executed", "pr.comment"]
    drained = asyncio.create_task(bus.drain())
    await asyncio.sleep(0.05)
    assert not drained.done(), "drain waits for the slow work"
    slow.set()
    await asyncio.wait_for(drained, 1)
    await bus.stop()


async def test_a_poll_tells_dashboards_of_changes_that_start_no_playbook(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    told: list[tuple[str, str | None]] = []
    notify = h.service.bus.notify

    def record(event: Event) -> None:
        told.append((event.kind, event.repo))
        notify(event)

    h.service.bus.notify = record  # type: ignore[method-assign]
    try:
        pr = make_pr()
        h.forge.put_pr(pr)
        await h.poll()
        told.clear()
        await h.poll()
        assert ("pr.refreshed", pr.repo) not in told, "nothing changed"
        thread = ReviewThread(
            id="T1", path="a.py", comments=[ThreadComment(id=1, author="bob", body="x")]
        )
        h.forge.put_pr(pr.model_copy(update={"threads": [thread]}))
        await h.poll()
        h.forge.put_pr(
            pr.model_copy(
                update={"threads": [thread.model_copy(update={"is_resolved": True})]}
            )
        )
        told.clear()
        events = await h.poll()
        assert events == [], "a resolved thread starts nothing"
        assert told.count(("pr.refreshed", pr.repo)) == 1
    finally:
        await h.close()
