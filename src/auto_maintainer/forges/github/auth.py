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

"""GitHub credentials: the token of the GitHub CLI login the service acts as.

The service writes with the token from ``token``. Agents get only the token from
``readonly_token``, which is ``None`` because a GitHub CLI token cannot be
down-scoped; agents work from checkouts the service makes instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from typing import Protocol

from ... import subproc

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "auto-maintainer"


class AuthError(RuntimeError):
    """No usable GitHub credential."""


class TokenProvider(Protocol):
    """Supplies bearer tokens for GitHub API calls."""

    async def token(self, repo: str | None) -> str:
        """A token that can act on ``repo`` (``owner/repo``, or an owner)."""
        ...

    async def readonly_token(self, repo: str | None) -> str | None:
        """A read-only token for agents working on ``repo``, or ``None`` if unavailable."""
        ...


class StaticTokenProvider:
    """A single personal or OAuth token.

    The token comes from ``token``, then the ``env_var`` environment variable, then
    ``gh auth token``. With ``env_var`` set to ``None`` only the GitHub CLI is asked.

    Args:
        env_var: Environment variable that holds the token, or ``None`` for gh only.
        token: An explicit token; takes precedence over the environment.
        gh_binary: The GitHub CLI used as a last resort.
    """

    def __init__(
        self,
        env_var: str | None = "GITHUB_TOKEN",
        *,
        token: str | None = None,
        gh_binary: str = "gh",
        gh_config_dir: str | None = None,
    ) -> None:
        self._env_var = env_var
        self._token = token
        self._gh = gh_binary
        self._gh_env = (
            {**os.environ, "GH_CONFIG_DIR": gh_config_dir} if gh_config_dir else None
        )
        self._lock = asyncio.Lock()

    async def token(self, repo: str | None = None) -> str:
        if self._token:
            return self._token
        async with self._lock:
            if self._token:
                return self._token
            from_env = os.environ.get(self._env_var, "") if self._env_var else ""
            value = from_env.strip() or await self._from_gh()
            if not value:
                raise AuthError(
                    "no GitHub token: log in with `gh auth login` on the host"
                    + (f" or set {self._env_var}" if self._env_var else "")
                )
            self._token = value
            return value

    async def readonly_token(self, repo: str | None = None) -> str | None:
        return None

    def forget(self) -> None:
        """Drop the token read before, so the next call reads the sign-in again
        (after a scope was added to it). An explicit token stays."""
        if self._env_var is not None or self._gh_env is not None:
            self._token = None

    async def _from_gh(self) -> str:
        binary = shutil.which(self._gh)
        if not binary:
            return ""
        proc = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._gh_env,
        )
        out, _ = await proc.communicate()
        return out.decode().strip() if proc.returncode == 0 else ""


class ScopeRequest:
    """Adding scopes to the GitHub CLI sign-in in ``gh_config_dir``: ``gh auth
    refresh``'s device flow, in which the signed-in account enters a one-time code on
    the forge and approves. The GitHub CLI refuses credentials of another account."""

    CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
    URL = re.compile(r"(https://\S+/login/device)")
    TIMEOUT = 900.0
    """A device code lives fifteen minutes."""

    def __init__(self, host: str, gh_config_dir: str | None, scopes: list[str]) -> None:
        self.host = host
        self.gh_config_dir = gh_config_dir
        self.scopes = scopes
        self.code: str | None = None
        self.url: str | None = None
        self.result: subproc.ProcResult | None = None
        self._shown = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, wait: float = 30) -> dict[str, str]:
        """Start the request; the one-time code and the page to enter it on."""
        binary = shutil.which("gh")
        if binary is None:
            raise AuthError("the GitHub CLI is not installed on this host")
        env = {
            **os.environ,
            "GH_PROMPT_DISABLED": "1",
            "BROWSER": "true",
            "NO_COLOR": "1",
            **({"GH_CONFIG_DIR": self.gh_config_dir} if self.gh_config_dir else {}),
        }
        argv = [binary, "auth", "refresh", "--hostname", self.host]
        for scope in self.scopes:
            argv += ["--scopes", scope]

        async def on_line(line: str) -> None:
            self.code = self.code or (
                m.group(1) if (m := self.CODE.search(line)) else None
            )
            self.url = self.url or (
                m.group(1) if (m := self.URL.search(line)) else None
            )
            if self.code and self.url:
                self._shown.set()

        async def run() -> None:
            self.result = await subproc.run(
                argv, env=env, stdin="\n", timeout=self.TIMEOUT, on_line=on_line
            )
            self._shown.set()

        self._task = asyncio.create_task(run())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shown.wait(), wait)
        if not (self.code and self.url):
            self.cancel()
            tail = (self.result.stderr or self.result.stdout) if self.result else ""
            raise AuthError(
                "the GitHub CLI did not start a scope request: "
                + " ".join(tail.strip().splitlines()[-2:])
            )
        return {"code": self.code, "url": self.url}

    async def wait(self) -> bool:
        """Whether the account approved, once the request ends."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        return bool(self.result and self.result.ok)

    def state(self) -> str:
        if self._task is None or not self._task.done():
            return "pending"
        return "done" if self.result and self.result.ok else "failed"

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
