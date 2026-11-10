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

"""The in-process event bus: events go to the engine and to live dashboard streams."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from ..domain.events import Event

log = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Implements ``EventSink``. Handlers run in order on one dispatcher task."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._handlers: list[Handler] = []
        self._listeners: set[asyncio.Queue[Event]] = set()
        self._task: asyncio.Task[None] | None = None
        self._busy = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._later: set[asyncio.Task[None]] = set()

    def later(self, work: Awaitable[None], name: str) -> None:
        """Run ``work`` beside the dispatcher, for a handler's slow step (a read from
        the forge): events are handed out one at a time, each after its handlers, so
        a slow handler would hold back every other event, and every dashboard's
        updates with them. ``drain`` waits for it as for the events."""
        self._busy += 1
        self._idle.clear()
        task = asyncio.create_task(self._run_later(work), name=name)
        self._later.add(task)
        task.add_done_callback(self._later.discard)

    async def _run_later(self, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:
            log.exception("background work after an event failed")
        finally:
            self._busy -= 1
            if self._busy == 0:
                self._idle.set()

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        self._busy += 1
        self._idle.clear()
        await self._queue.put(event)
        if self._task is None:
            self.start()

    def notify(self, event: Event) -> None:
        """Hand ``event`` to the dashboard's event streams only: no handler, no job,
        and no store read. For frequent updates such as a job's progress."""
        for listener in list(self._listeners):
            if listener.qsize() < 1000:
                listener.put_nowait(event)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="event-bus")

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                for handler in self._handlers:
                    try:
                        await handler(event)
                    except Exception:
                        log.exception("event handler failed on %s", event.kind)
                for listener in list(self._listeners):
                    if listener.qsize() < 1000:
                        listener.put_nowait(event)
            finally:
                self._busy -= 1
                if self._busy == 0:
                    self._idle.set()

    async def drain(self) -> None:
        await self._idle.wait()

    async def stop(self) -> None:
        """Stop dispatching and end the work running beside it (``later``), waiting
        until that work has unwound: it may hold a store connection, which must be
        given back before the store closes, or closing it later never finishes."""
        later = list(self._later)
        for task in later:
            task.cancel()
        if later:
            await asyncio.gather(*later, return_exceptions=True)
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def stream(
        self, closing: asyncio.Event | None = None, heartbeat: float = 15.0
    ) -> AsyncIterator[Event | None]:
        """Events published from now on, for a server-sent event stream.

        Yields ``None`` after ``heartbeat`` seconds without an event, so the caller
        can check that its client is still there, and ends once ``closing`` is set,
        so a server shutting down never waits on an idle dashboard.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._listeners.add(queue)
        get: asyncio.Future[Event] | None = None
        stop: asyncio.Future[bool] | None = None
        try:
            while closing is None or not closing.is_set():
                get = asyncio.ensure_future(queue.get())
                stop = asyncio.ensure_future(closing.wait()) if closing else None
                waiting = {get} if stop is None else {get, stop}
                done, _ = await asyncio.wait(
                    waiting, timeout=heartbeat, return_when=asyncio.FIRST_COMPLETED
                )
                if stop is not None:
                    stop.cancel()
                if get in done:
                    yield get.result()
                    continue
                get.cancel()
                if closing is not None and closing.is_set():
                    return
                yield None
        finally:
            # A client that leaves closes this generator mid-wait; cancel what it was
            # waiting on so no pending task is left behind.
            for pending in (get, stop):
                if pending is not None and not pending.done():
                    pending.cancel()
            self._listeners.discard(queue)
