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

"""Cron schedules, the poll loop, and housekeeping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from croniter import croniter

from ..domain.clock import Clock
from ..domain.events import Event, EventKind
from ..domain.ports import EventSink

log = logging.getLogger(__name__)

Task = Callable[[], Awaitable[object]]


class Scheduler:
    """Fires ``schedule.tick`` events and runs built-in housekeeping on its own loop."""

    def __init__(
        self,
        schedules: dict[str, str],
        bus: EventSink,
        clock: Clock,
        builtins: dict[str, Task] | None = None,
        every_minute: list[Task] | None = None,
        poll: Task | None = None,
        poll_seconds: int = 120,
    ) -> None:
        for name, expr in schedules.items():
            if not croniter.is_valid(expr):
                raise ValueError(
                    f"schedule {name!r} has an invalid cron expression {expr!r}"
                )
        self.schedules = schedules
        self.bus = bus
        self.clock = clock
        self.builtins = builtins or {}
        self.every_minute = every_minute or []
        self.poll = poll
        self.poll_seconds = poll_seconds
        self._next: dict[str, datetime] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def due(self, now: datetime) -> list[str]:
        """Schedule names due at ``now``; advances each one's next fire time."""
        names = []
        for name, expr in self.schedules.items():
            nxt = self._next.get(name)
            if nxt is None:
                self._next[name] = croniter(expr, now).get_next(datetime)
                continue
            if now >= nxt:
                names.append(name)
                self._next[name] = croniter(expr, now).get_next(datetime)
        return names

    async def fire(self, name: str) -> None:
        builtin = self.builtins.get(name)
        if builtin is not None:
            try:
                await builtin()
            except Exception:
                log.exception("scheduled task %s failed", name)
        await self.bus.publish(
            Event(
                kind=EventKind.SCHEDULE_TICK, payload={"name": name}, source="scheduler"
            )
        )

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._cron_loop(), name="scheduler"))
        if self.poll is not None:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="poller"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _cron_loop(self) -> None:
        while True:
            now = self.clock.now()
            for name in self.due(now):
                await self.fire(name)
            for task in self.every_minute:
                try:
                    await task()
                except Exception:
                    log.exception("housekeeping failed")
            await asyncio.sleep(60 - now.second if now.second < 60 else 60)

    async def _poll_loop(self) -> None:
        assert self.poll is not None
        while True:
            try:
                await self.poll()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(self.poll_seconds)
