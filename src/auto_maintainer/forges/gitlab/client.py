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

"""GitLab's REST API (v4), with the token of the GitLab CLI's login.

``GlabToken`` reads the token the GitLab CLI (``glab``) keeps for a host, from its
own configuration folder. An OAuth sign-in (``glab auth login --device``) expires in
hours; ``glab`` renews it whenever it calls the API, so the token is read through one
cheap ``glab api user`` now and then. The token goes into request headers only,
never into a command line, a URL, or a log.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from ...domain.forges import parse_repo
from ...subproc import run

TokenFn = Callable[[], Awaitable[str | None]]
REFRESH_SECONDS = 300.0
"""How long a token read from ``glab`` is used before ``glab`` is asked again."""


class GitLabError(RuntimeError):
    """GitLab answered with an error; ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"GitLab {status}: {message}")
        self.status = status
        self.body = body


class GlabToken:
    """The token of the GitLab CLI's login for one host.

    Args:
        host: The GitLab host, as ``glab`` knows it (``--hostname``).
        config_dir: ``GLAB_CONFIG_DIR`` of the login; ``None`` for the host user's.
        glab: The GitLab CLI.
        token: A fixed token instead of ``glab``'s (a person's own).
    """

    def __init__(
        self,
        host: str,
        config_dir: str | None = None,
        *,
        glab: str = "glab",
        token: str | None = None,
    ) -> None:
        self.host = host
        self.config_dir = config_dir
        self.glab = glab
        self._fixed = token
        self._cached: tuple[float, str | None] | None = None
        self._lock = asyncio.Lock()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITLAB_")}
        env.pop("GITLAB_TOKEN", None)
        if self.config_dir:
            env["GLAB_CONFIG_DIR"] = os.path.expanduser(self.config_dir)
        env["GLAB_NO_PROMPT"] = "1"
        return env

    async def token(self, repo: str | None = None) -> str | None:
        if self._fixed is not None:
            return self._fixed
        async with self._lock:
            now = time.monotonic()
            if self._cached and now - self._cached[0] < REFRESH_SECONDS:
                return self._cached[1]
            env = self._env()
            # An API call lets glab renew an expired OAuth token before it is read.
            await run(
                [self.glab, "api", "user", "--hostname", self.host],
                env=env,
                timeout=60,
            )
            got = await run(
                [self.glab, "config", "get", "token", "--host", self.host],
                env=env,
                timeout=30,
            )
            value = got.stdout.strip() if got.ok else ""
            self._cached = (now, value or None)
            return self._cached[1]


def project_id(repo: str) -> str:
    """The URL form of ``repo``'s project on its forge: its path, encoded."""
    return quote(parse_repo(repo).path, safe="")


class GitLabClient:
    """A small client for GitLab's REST API.

    Args:
        api_url: ``https://<host>/api/v4``.
        token: Returns the token; ``None`` reads as anonymous (public projects).
        transport: For tests.
    """

    def __init__(
        self,
        api_url: str,
        token: TokenFn | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._http = httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            transport=transport,
            headers={"User-Agent": "auto-maintainer"},
            follow_redirects=True,
        )

    @property
    def anonymous(self) -> bool:
        """Without a token: public projects only, and not their notes on gitlab.com."""
        return self._token is None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        token = await self._token() if self._token else None
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        allow: tuple[int, ...] = (),
        raw: bool = False,
    ) -> Any:
        """Send a request; ``None`` for a status in ``allow``, else the JSON answer
        (or the text with ``raw``). Raises ``GitLabError`` for other errors."""
        for attempt in range(4):
            r = await self._http.request(
                method,
                f"{self.api_url}{path}",
                params=params,
                json=json,
                headers=await self._headers(),
            )
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(
                    min(30.0, float(r.headers.get("Retry-After", 2**attempt)))
                )
                continue
            break
        if r.status_code in allow:
            return None
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text[:500]
            message = (
                body.get("message") or body.get("error")
                if isinstance(body, dict)
                else body
            )
            raise GitLabError(r.status_code, str(message)[:300], body)
        if raw:
            return r.text
        if not r.content:
            return None
        return r.json()

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json=json, **kw)

    async def put(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("PUT", path, json=json, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 2000,
        allow: tuple[int, ...] = (),
    ) -> list[Any]:
        """Every item of a list, following GitLab's page headers, up to ``limit``;
        nothing for a status in ``allow``."""
        items: list[Any] = []
        page = 1
        while page and len(items) < limit:
            for attempt in range(4):
                r = await self._http.get(
                    f"{self.api_url}{path}",
                    params={**(params or {}), "per_page": 100, "page": page},
                    headers=await self._headers(),
                )
                if r.status_code != 429 and r.status_code < 500:
                    break
                wait = float(r.headers.get("Retry-After", 2**attempt))
                await asyncio.sleep(min(30.0, wait))
            if r.status_code in allow:
                return items
            if r.status_code >= 400:
                raise GitLabError(r.status_code, r.text[:300])
            items.extend(r.json())
            nxt = r.headers.get("X-Next-Page", "")
            page = int(nxt) if nxt.isdigit() else 0
        return items[:limit]
