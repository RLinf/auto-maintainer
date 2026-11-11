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

"""An agent CLI's own sign-in, run on the host for someone on the dashboard.

The CLIs sign in without a browser on their host in one of two ways: they print a
page to open and a one-time code to enter there (Codex's device login), or a page
whose result is a code the person pastes back (Claude Code, Antigravity). A
``LoginProcess`` runs the CLI's sign-in command with the account's home folder,
reads what it prints into an ``AgentLogin`` for the dashboard, and writes the pasted
code to the CLI's standard input, nowhere else. The CLI keeps the sign-in in that
home folder, as it would for a person at a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from ..domain.agents import AgentLogin
from ..domain.secrets import redact_secrets

LOGIN_SECONDS = 900.0
"""How long a sign-in may wait for the person; the codes expire in about as long."""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL = re.compile(r"https://[^\s\"'<>]+")
_DEVICE_CODE = re.compile(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4,6})\s*$")
_ASKS = re.compile(r"\bpaste\b", re.IGNORECASE)


class LoginProcess:
    """One CLI sign-in. ``state`` is what the dashboard shows.

    Args:
        argv: The sign-in command.
        env: Its environment, with ``HOME`` set to the account's home folder.
        state: The record to fill in.
        marker: The file the CLI writes when the sign-in succeeds. Once it is new,
            the sign-in is done and the CLI is stopped, since one may stay running
            after it (Antigravity's ``acp --login`` serves ACP next).
        end: Called once the sign-in is over, to remove its container.
    """

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        state: AgentLogin,
        marker: Path | None = None,
        end: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._end = end
        self.argv = list(argv)
        self.env = dict(env)
        self.state = state
        self.marker = marker
        self._before = _mtime(marker)
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._lines: list[str] = []

    def start(self, cwd: Path) -> None:
        self._task = asyncio.create_task(self._run(cwd), name=f"login-{self.state.id}")

    async def wait(self) -> AgentLogin:
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        return self.state

    async def submit(self, code: str) -> None:
        """Hand the code the sign-in page showed to the waiting CLI."""
        proc = self._proc
        if proc is None or proc.stdin is None or self.state.state != "waiting":
            raise RuntimeError("this sign-in is not waiting for a code")
        proc.stdin.write(code.strip().encode() + b"\n")
        await proc.stdin.drain()
        self.state.needs_code = False
        self.state.message = "checking the code"

    async def cancel(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGTERM)
        self.state.state = "cancelled"

    async def _run(self, cwd: Path) -> None:
        try:
            await self._sign_in(cwd)
        finally:
            if self._end is not None:
                with contextlib.suppress(Exception):
                    await self._end()

    async def _sign_in(self, cwd: Path) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self.env,
                start_new_session=True,
            )
        except OSError as exc:
            self.state.state, self.state.message = "failed", f"could not start: {exc}"
            return
        self.state.state = "waiting"
        reader = asyncio.create_task(self._read())
        ended = asyncio.create_task(self._proc.wait())
        watched = asyncio.create_task(self._signed())
        try:
            await asyncio.wait(
                {ended, watched},
                timeout=LOGIN_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            watched.cancel()
        if not ended.done():
            signed = watched.done() and not watched.cancelled() and watched.result()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), 10)
            reader.cancel()
            if self.state.state == "cancelled":
                return
            if not signed:
                self.state.state, self.state.message = "failed", "the sign-in timed out"
                return
            self.state.state, self.state.message = "done", "signed in"
            self.state.needs_code = False
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader, 5)
        code = ended.result()
        if self.state.state == "cancelled":
            return
        tail = " ".join(self._lines[-3:])
        if code == 0 or self._marked():
            self.state.state, self.state.message = "done", tail or "signed in"
        else:
            self.state.state = "failed"
            self.state.message = tail or f"the sign-in ended with exit code {code}"
        self.state.needs_code = False

    def _marked(self) -> bool:
        """Whether the CLI wrote its sign-in since this one started."""
        now = _mtime(self.marker)
        return now is not None and (self._before is None or now > self._before)

    async def _signed(self) -> bool:
        """Until the CLI's sign-in file is new: ``True`` then; never without one."""
        if self.marker is None:
            await asyncio.Event().wait()
        while not self._marked():
            await asyncio.sleep(1)
        return True

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        # The prompt to paste a code ends without a newline, so read what comes.
        buffer = ""
        while chunk := await self._proc.stdout.read(4096):
            buffer += chunk.decode("utf-8", "replace")
            *lines, buffer = buffer.split("\n")
            for line in [*lines, buffer] if _ASKS.search(buffer) else lines:
                self._take(line)
            if _ASKS.search(buffer):
                buffer = ""
        if buffer:
            self._take(buffer)

    def _take(self, raw: str) -> None:
        line = _ANSI.sub("", raw).strip()
        if not line:
            return
        if not self.state.url and (found := _URL.search(line)):
            self.state.url = found.group(0).rstrip(".,)")
            return
        if self.state.url and not self.state.code and (m := _DEVICE_CODE.match(line)):
            self.state.code = m.group(1)
            return
        if _ASKS.search(line):
            self.state.needs_code = True
        text = redact_secrets(line)[0][:300]
        self._lines = [*self._lines[-9:], text]
        self.state.message = text


def _mtime(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path is not None else None
    except OSError:
        return None
