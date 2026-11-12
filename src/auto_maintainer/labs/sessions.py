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

"""The door from an agent to its reproduction session on a test machine (design 5.10).

The service serves this on a Unix socket that the agent of a reproduction gets in its
container. The agent's ``lab`` command posts one command at a time with its session's
token; ``LabExecutor.run_in_session`` checks it against the machine's rules and runs
it in the session's run folder on the machine, and its output streams back line by
line, then a last record with the exit code. Nothing but an open session's token
gets a command run, and the agent never holds the machine's SSH key. The server also
closes sessions past their time limit, and on start cleans up the sessions a
previous process left open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .executor import LabExecutor

log = logging.getLogger(__name__)

END = "\x1e"
"""Starts the last line of a command's output: a JSON record with its exit code."""

JANITOR_SECONDS = 30


class LabSessionServer:
    """Serves ``POST /run``, ``POST /sync``, ``POST /gpus``, and ``GET /session`` on
    ``socket_path``."""

    def __init__(self, socket_path: Path, labs: LabExecutor) -> None:
        self.socket_path = socket_path
        self.labs = labs
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._janitor: asyncio.Task[None] | None = None
        self.app = Starlette(
            routes=[
                Route("/run", self._run, methods=["POST"]),
                Route("/session", self._session, methods=["GET"]),
                Route("/sync", self._sync, methods=["POST"]),
                Route("/gpus", self._gpus, methods=["POST"]),
            ]
        )

    async def start(self) -> None:
        with contextlib.suppress(Exception):
            cleaned = await self.labs.recover_sessions()
            if cleaned:
                log.info("cleaned up %d reproduction sessions left open", cleaned)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                uds=str(self.socket_path),
                log_level="warning",
                lifespan="off",
                access_log=False,
                timeout_keep_alive=600,
            )
        )
        self._task = asyncio.create_task(self._server.serve(), name="lab-sessions")
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if self.socket_path.exists():
            self.socket_path.chmod(0o600)
        self._janitor = asyncio.create_task(self._sweep(), name="lab-janitor")

    async def stop(self) -> None:
        if self._janitor is not None:
            self._janitor.cancel()
            with contextlib.suppress(BaseException):
                await self._janitor
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(JANITOR_SECONDS)
            with contextlib.suppress(Exception):
                await self.labs.expire_sessions()

    async def _session(self, request: Request) -> Response:
        info = self.labs.session_info(request.headers.get("x-lab-session", ""))
        if info is None:
            return JSONResponse({"error": "no open session for this token"}, 403)
        return JSONResponse(info)

    async def _sync(self, request: Request) -> Response:
        token = request.headers.get("x-lab-session", "")
        if self.labs.session_info(token) is None:
            return JSONResponse({"error": "no open session for this token"}, 403)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        try:
            out = await self.labs.sync_session(token, str(body.get("side") or "head"))
        except Exception as e:  # reported to the agent, not raised into the server
            log.exception("copying a checkout to a test machine failed")
            out = {"error": f"the copy failed: {e}"}
        return JSONResponse(out)

    async def _gpus(self, request: Request) -> Response:
        """``{"count": N, "wait_minutes": M}``: hold N GPUs, waiting up to M minutes
        for enough to be idle (``LabExecutor.grow_gpus``)."""
        token = request.headers.get("x-lab-session", "")
        if self.labs.session_info(token) is None:
            return JSONResponse({"error": "no open session for this token"}, 403)
        try:
            body = json.loads(await request.body() or b"{}")
            count = int(body["count"])
            wait = int(float(body.get("wait_minutes") or 0) * 60)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'send {"count": N}'}, 400)
        try:
            out = await self.labs.grow_gpus(token, count, wait)
        except Exception as e:  # reported to the agent, not raised into the server
            log.exception("leasing GPUs for a session failed")
            out = {"error": f"could not lease GPUs: {e}"}
        return JSONResponse(out)

    async def _run(self, request: Request) -> Response:
        token = request.headers.get("x-lab-session", "")
        if self.labs.session_info(token) is None:
            return JSONResponse({"error": "no open session for this token"}, 403)
        try:
            body = json.loads(await request.body() or b"{}")
            command = str(body["command"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'send {"command": ...}'}, 400)
        side = str(body.get("side") or "head")
        minutes = body.get("timeout_minutes")
        timeout = int(float(minutes) * 60) if minutes else None
        lines: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_line(line: str) -> None:
            await lines.put(line)

        async def work() -> dict[str, Any]:
            try:
                return await self.labs.run_in_session(
                    token, command, side=side, timeout_seconds=timeout, on_line=on_line
                )
            except Exception as e:  # reported to the agent, not raised into the server
                log.exception("a reproduction command failed to run")
                return {"error": f"the command could not run: {e}"}
            finally:
                await lines.put(None)

        job = asyncio.create_task(work())

        async def stream() -> AsyncIterator[bytes]:
            while True:
                line = await lines.get()
                if line is None:
                    break
                yield (line + "\n").encode()
            yield (END + json.dumps(await job) + "\n").encode()

        return StreamingResponse(stream(), media_type="text/plain")
