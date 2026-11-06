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
import os
import shutil
from typing import Protocol

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
