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

A turn of the chat, a review's conversation, or the People conversation runs its
agent inside the request that sent the message. ``LiveTurns.run`` runs that agent
as a task of its own, by the turn's run id (``ChatThread.live_run``); ``stop``
cancels it, which ends the agent's process, and the request then answers with
``Stopped`` instead of the agent's answer, so the conversation records who stopped
it and takes the next message at once.
"""

from __future__ import annotations

import asyncio
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
        self._stopped: dict[str, str] = {}

    async def run(self, run_id: str, work: Awaitable[T]) -> T | Stopped:
        """Await ``work`` as the turn ``run_id``: its result, or ``Stopped`` when
        someone stopped it. A request that is itself cancelled cancels the work."""
        task = asyncio.ensure_future(work)
        self._runs[run_id] = task  # type: ignore[assignment]
        try:
            return await task
        except asyncio.CancelledError:
            by = self._stopped.pop(run_id, None)
            if task.cancelled() and by is not None:
                return Stopped(by=by)
            task.cancel()
            raise
        finally:
            self._runs.pop(run_id, None)
            self._stopped.pop(run_id, None)

    def running(self, run_id: str | None) -> bool:
        task = self._runs.get(run_id or "")
        return task is not None and not task.done()

    async def stop(self, run_id: str | None, by: str) -> bool:
        """Stop the turn ``run_id`` for ``by``; whether one was running."""
        task = self._runs.get(run_id or "")
        if task is None or task.done():
            return False
        self._stopped[run_id or ""] = by
        task.cancel()
        await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)
        return True
