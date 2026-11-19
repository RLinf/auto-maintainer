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

"""Conversation turns whose agent runs now, so a person can stop one.

A turn of the chat runs as a background task started by ``LiveTurns.start`` so the
POST that sent the message returns at once; a review's or People conversation still
runs inside its request. Either way, ``LiveTurns.run`` runs the turn's work as a
task of its own by the turn's run id (``ChatThread.live_run``); ``stop`` cancels it
and waits for the turn to unwind, answering with ``Stopped`` instead of the agent's
answer so the conversation records who stopped it and takes the next message at once.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")

STOP_WAIT_SECONDS = 30
"""How long ``stop`` waits for the agent's process to end."""


@dataclass(frozen=True)
class Stopped:
    """A turn someone stopped before its agent answered."""

    by: str


class LiveTurns:
    def __init__(self) -> None:
        self._runs: dict[str, asyncio.Future[object]] = {}
        self._bg: dict[str, asyncio.Task[object]] = {}
        self._finished_run: set[str] = set()
        self._stopped: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, thread_id: str) -> asyncio.Lock:
        """Per-thread lock serializing submission, deduplication, and stop handoffs."""
        return self._locks.setdefault(thread_id, asyncio.Lock())

    def start(self, run_id: str, work: Awaitable[object]) -> asyncio.Task[object]:
        """Run ``work`` in the background as turn ``run_id``.

        ``work`` should call ``run(run_id, ...)`` as its first awaitable step so
        ``stop`` cancels it cleanly without racing against startup.
        """
        task = asyncio.ensure_future(work)
        self._bg[run_id] = task

        def _done(_t: asyncio.Task[object]) -> None:
            if self._bg.get(run_id) is task:
                self._bg.pop(run_id, None)
            self._finished_run.discard(run_id)
            self._stopped.pop(run_id, None)

        task.add_done_callback(_done)
        return task

    async def run(self, run_id: str, work: Awaitable[T]) -> T | Stopped:
        """Await ``work`` as the turn ``run_id``: its result, or ``Stopped`` when
        someone stopped it. A request that is itself cancelled cancels the work."""
        by = self._stopped.pop(run_id, None)
        if by is not None:
            if asyncio.iscoroutine(work):
                work.close()
            if run_id in self._bg:
                self._finished_run.add(run_id)
            return Stopped(by=by)
        task = asyncio.ensure_future(work)
        self._runs[run_id] = task  # type: ignore[assignment]
        try:
            return await task
        except asyncio.CancelledError:
            by = self._stopped.pop(run_id, None)
            if task.cancelled() and by is not None:
                return Stopped(by=by)
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        finally:
            self._runs.pop(run_id, None)
            if run_id in self._bg:
                self._finished_run.add(run_id)
            else:
                self._stopped.pop(run_id, None)

    def running(self, run_id: str | None) -> bool:
        if not run_id:
            return False
        for mapping in (self._bg, self._runs):
            task = mapping.get(run_id)
            if task is not None and not task.done():
                return True
        return False

    async def stop(self, run_id: str | None, by: str) -> bool:
        """Stop the turn ``run_id`` for ``by``; whether one was running."""
        if not run_id:
            return False
        bg = self._bg.get(run_id)
        inner = self._runs.get(run_id)
        if inner is not None and not inner.done():
            self._stopped[run_id] = by
            inner.cancel()
            wait_for = bg if bg is not None and not bg.done() else inner
            await asyncio.wait({wait_for}, timeout=STOP_WAIT_SECONDS)
            return True
        if bg is not None and not bg.done():
            if run_id in self._finished_run or run_id in self._stopped:
                await asyncio.wait({bg}, timeout=STOP_WAIT_SECONDS)
                return False
            self._stopped[run_id] = by
            await asyncio.wait({bg}, timeout=STOP_WAIT_SECONDS)
            return True
        return False

    async def wait(
        self, run_id: str | None = None, timeout: float | None = None
    ) -> None:
        """Wait for one background turn (or every active one) to finish."""
        if run_id is not None:
            task = self._bg.get(run_id)
            if task is not None and not task.done():
                await asyncio.wait({task}, timeout=timeout)
            return
        while True:
            tasks = [t for t in self._bg.values() if not t.done()]
            if not tasks:
                return
            await asyncio.wait(tasks, timeout=timeout)
            if timeout is not None:
                return

    async def stop_all(self) -> None:
        """Cancel every active background turn on shutdown and wait for it to unwind."""
        tasks = [t for t in self._bg.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
