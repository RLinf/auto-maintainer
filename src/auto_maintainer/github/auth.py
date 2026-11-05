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

"""GitHub credentials: a static token, or GitHub App installation tokens.

The service writes with the token from ``token``. Agents get only the token from
``readonly_token``, which is ``None`` in static-token mode because a personal
token cannot be down-scoped.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

import httpx
import jwt

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "auto-maintainer"

READONLY_PERMISSIONS = {
    "metadata": "read",
    "contents": "read",
    "pull_requests": "read",
    "issues": "read",
    "checks": "read",
    "actions": "read",
}

_REFRESH_MARGIN = 300.0
"""Seconds before expiry at which a cached installation token is replaced."""


class AuthError(RuntimeError):
    """No usable GitHub credential."""


class TokenProvider(Protocol):
    """Supplies bearer tokens for GitHub API calls."""

    mode: Literal["token", "app"]

    async def token(self, repo: str | None) -> str:
        """A token that can act on ``repo`` (``owner/repo``, or an owner)."""
        ...

    async def readonly_token(self, repo: str | None) -> str | None:
        """A read-only token for agents working on ``repo``, or ``None`` if unavailable."""
        ...


class StaticTokenProvider:
    """A single personal or OAuth token.

    The token comes from ``token``, then the ``env_var`` environment variable, then
    ``gh auth token``.

    Args:
        env_var: Environment variable that holds the token.
        token: An explicit token; takes precedence over the environment.
        gh_binary: The GitHub CLI used as a last resort.
    """

    mode: Literal["token", "app"] = "token"

    def __init__(
        self,
        env_var: str = "GITHUB_TOKEN",
        *,
        token: str | None = None,
        gh_binary: str = "gh",
    ) -> None:
        self._env_var = env_var
        self._token = token
        self._gh = gh_binary
        self._lock = asyncio.Lock()

    async def token(self, repo: str | None = None) -> str:
        if self._token:
            return self._token
        async with self._lock:
            if self._token:
                return self._token
            value = os.environ.get(self._env_var, "").strip() or await self._from_gh()
            if not value:
                raise AuthError(
                    f"no GitHub token: set {self._env_var} or log in with `gh auth login`"
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
        )
        out, _ = await proc.communicate()
        return out.decode().strip() if proc.returncode == 0 else ""


class GitHubAppTokenProvider:
    """Installation tokens for a GitHub App.

    Installation IDs are cached per account and tokens per installation (and, for
    read-only tokens, per repository) until five minutes before they expire.

    Args:
        app_id: The App's ID.
        private_key_pem: The App's private key in PEM form.
        api_url: The REST API root.
        http: An HTTP client to use; one is created when omitted.
        clock: Returns the current Unix time; for tests.
    """

    mode: Literal["token", "app"] = "app"

    def __init__(
        self,
        app_id: str | int,
        private_key_pem: str | bytes,
        api_url: str = API_URL,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = str(app_id)
        self._key = private_key_pem
        self._api = api_url.rstrip("/")
        self._http = http or httpx.AsyncClient(trust_env=True, timeout=30.0)
        self._clock = clock
        self._jwt: tuple[str, float] | None = None
        self._installations: dict[str, int] = {}
        self._tokens: dict[tuple[int, str | None], tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def app_jwt(self) -> str:
        """A JWT that authenticates as the App itself, valid for about nine minutes."""
        now = self._clock()
        if self._jwt and self._jwt[1] - 60 > now:
            return self._jwt[0]
        exp = now + 540
        token = jwt.encode(
            {"iat": int(now) - 60, "exp": int(exp), "iss": self._app_id},
            self._key,
            algorithm="RS256",
        )
        self._jwt = (token, exp)
        return token

    async def token(self, repo: str | None) -> str:
        return await self._installation_token(repo, readonly=False)

    async def readonly_token(self, repo: str | None) -> str | None:
        return await self._installation_token(repo, readonly=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    async def _installation_id(self, target: str) -> int:
        owner = target.split("/", 1)[0]
        if owner in self._installations:
            return self._installations[owner]
        paths = (
            [f"/repos/{target}/installation"]
            if "/" in target
            else [
                f"/orgs/{owner}/installation",
                f"/users/{owner}/installation",
            ]
        )
        for path in paths:
            resp = await self._http.get(self._api + path, headers=self._headers())
            if resp.status_code == 200:
                self._installations[owner] = int(resp.json()["id"])
                return self._installations[owner]
            if resp.status_code != 404:
                raise AuthError(
                    f"GitHub App installation lookup for {target} failed: HTTP {resp.status_code}"
                )
        raise AuthError(f"the GitHub App is not installed on {target}")

    async def _installation_token(self, repo: str | None, *, readonly: bool) -> str:
        if not repo:
            raise AuthError("GitHub App tokens need a repository or owner")
        async with self._lock:
            installation = await self._installation_id(repo)
            scope = repo if readonly else None
            key = (installation, f"ro:{scope}" if readonly else None)
            cached = self._tokens.get(key)
            if cached and cached[1] - _REFRESH_MARGIN > self._clock():
                return cached[0]
            body: dict[str, object] = {}
            if readonly:
                body["permissions"] = READONLY_PERMISSIONS
                if scope and "/" in scope:
                    body["repositories"] = [scope.split("/", 1)[1]]
            resp = await self._http.post(
                f"{self._api}/app/installations/{installation}/access_tokens",
                headers=self._headers(),
                json=body or None,
            )
            if resp.status_code != 201:
                raise AuthError(
                    f"creating an installation token for {repo} failed: HTTP {resp.status_code}"
                )
            data = resp.json()
            expires = datetime.fromisoformat(
                data["expires_at"].replace("Z", "+00:00")
            ).timestamp()
            self._tokens[key] = (data["token"], expires)
            return data["token"]
