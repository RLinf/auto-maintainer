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

"""Costly dashboard answers (the PR list, the agents' work), shared while nothing
changed.

Every open tab reloads these lists on the live events it hears, and the tabs hear
each event at the same moment: without this, each reload computes the same answer
again on the one event loop that serves every page and runs the service. An answer
is kept under its question (the same person, the same parameters, the repositories
they see) and the ``version`` it was asked at, which grows with every event the
service publishes or announces and with every change made through the API: a
question asked while the same answer is being computed waits for it, and one asked
within ``seconds`` of it gets it, until the version changes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

from fastapi import Response
from fastapi.encoders import jsonable_encoder

MAX_KEYS = 256


class Recent:
    def __init__(self, seconds: float, events: Callable[[], int]) -> None:
        self.seconds = seconds
        self._events = events
        self._writes = 0
        self._done: dict[Hashable, tuple[float, bytes]] = {}
        self._running: dict[Hashable, asyncio.Future[bytes]] = {}
        self._values: dict[Hashable, tuple[float, Any]] = {}
        self._computing: dict[Hashable, asyncio.Future[Any]] = {}

    def wrote(self) -> None:
        """A request changed something: no kept answer is served again."""
        self._writes += 1

    def version(self) -> tuple[int, int]:
        return (self._events(), self._writes)

    async def json(
        self, key: Hashable, compute: Callable[[], Awaitable[Any]]
    ) -> Response:
        """The JSON answer to ``key``: kept, being computed, or computed now."""
        full = (key, self.version())
        hit = self._done.get(full)
        if hit is not None and time.monotonic() - hit[0] < self.seconds:
            return _response(hit[1])
        running = self._running.get(full)
        if running is None:
            running = asyncio.ensure_future(_encode(compute))
            self._running[full] = running
            running.add_done_callback(lambda _: self._kept(full, running))
        # Shielded: a client that leaves stops only its own wait.
        return _response(await asyncio.shield(running))

    def _kept(self, full: Hashable, done: asyncio.Future[bytes]) -> None:
        if self._running.get(full) is done:
            del self._running[full]
        if done.cancelled() or done.exception() is not None:
            return
        now = time.monotonic()
        if len(self._done) >= MAX_KEYS:
            for old in [
                k for k, (at, _) in self._done.items() if now - at >= self.seconds
            ]:
                del self._done[old]
            if len(self._done) >= MAX_KEYS:
                self._done.clear()
        self._done[full] = (now, done.result())

    async def shared(
        self,
        key: Hashable,
        compute: Callable[[], Awaitable[Any]],
        seconds: float | None = None,
    ) -> Any:
        """The value of ``key`` as ``json`` keeps an answer, for a page that adds
        each person's own fields to what everyone shares: one computation serves
        every tab asking at the same version within ``seconds`` (``self.seconds``
        when unset). Callers must not change what it returns."""
        full = (key, self.version())
        window = self.seconds if seconds is None else seconds
        hit = self._values.get(full)
        if hit is not None and time.monotonic() - hit[0] < window:
            return hit[1]
        running = self._computing.get(full)
        if running is None:
            running = asyncio.ensure_future(compute())
            self._computing[full] = running
            running.add_done_callback(lambda _: self._kept_value(full, running))
        return await asyncio.shield(running)

    def _kept_value(self, full: Hashable, done: asyncio.Future[Any]) -> None:
        if self._computing.get(full) is done:
            del self._computing[full]
        if done.cancelled() or done.exception() is not None:
            return
        if len(self._values) >= MAX_KEYS:
            self._values.clear()
        self._values[full] = (time.monotonic(), done.result())


async def _encode(compute: Callable[[], Awaitable[Any]]) -> bytes:
    # As FastAPI's JSONResponse writes it.
    return json.dumps(
        jsonable_encoder(await compute()),
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def _response(body: bytes) -> Response:
    return Response(content=body, media_type="application/json")
