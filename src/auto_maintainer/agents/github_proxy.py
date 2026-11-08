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

"""A door to GitHub that only reads, for agents in their containers (design 5.7).

An agent's container holds no GitHub token. The service serves this proxy on a Unix
socket that each container mounts, and ``gh`` there sends every request through it
(``http_unix_socket``). The proxy adds the service's own token to requests for the
GitHub API and lets through only what reads: GET, HEAD, and OPTIONS, and GraphQL
queries (``gh`` reads PRs through GraphQL). A mutation and every other method are
refused, so an agent reads GitHub as the service without being able to write; writes
stay with the actuator and a maintainer's approval. The token never enters a
container.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

READS = frozenset({"GET", "HEAD", "OPTIONS"})
GRAPHQL_PATHS = frozenset({"/graphql", "/api/graphql"})
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
REFUSED = (
    "Agents read GitHub only: this request would change something. Writing goes "
    "through the service and a maintainer's approval."
)


def graphql_reads(document: str) -> bool:
    """Whether a GraphQL document holds no mutation or subscription: its top-level
    definitions are queries and fragments. Strings and comments are skipped."""
    depth = 0
    i, n = 0, len(document)
    while i < n:
        c = document[i]
        if c == "#":
            while i < n and document[i] != "\n":
                i += 1
            continue
        if c == '"':
            if document.startswith('"""', i):
                end = document.find('"""', i + 3)
                i = n if end < 0 else end + 3
                continue
            i += 1
            while i < n and document[i] != '"':
                i += 2 if document[i] == "\\" else 1
            i += 1
            continue
        if c in "{(":
            depth += 1
        elif c in "})":
            depth -= 1
        elif depth == 0 and (c.isalpha() or c == "_"):
            start = i
            while i < n and (document[i].isalnum() or document[i] == "_"):
                i += 1
            if document[start:i] in ("mutation", "subscription"):
                return False
            continue
        i += 1
    return True


def allowed(method: str, host: str, path: str, body: bytes, api_host: str) -> bool:
    """Whether the proxy forwards this request: it only reads."""
    if method in READS:
        return True
    if method != "POST" or host != api_host or path not in GRAPHQL_PATHS:
        return False
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return False
    queries = payload if isinstance(payload, list) else [payload]
    return bool(queries) and all(
        isinstance(q, dict)
        and isinstance(q.get("query"), str)
        and graphql_reads(q["query"])
        for q in queries
    )


class GitHubReadProxy:
    """Serves the read-only door on ``socket_path`` while the service runs.

    Args:
        socket_path: The Unix socket containers mount.
        token: The service's GitHub token.
        api_host: The GitHub API host; only its requests carry the token.
        transport: For tests, the transport upstream requests go through.
    """

    def __init__(
        self,
        socket_path: Path,
        token: Callable[[], Awaitable[str | None]],
        *,
        api_host: str = "api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._token = token
        self.api_host = api_host
        self._client = httpx.AsyncClient(
            transport=transport, timeout=60, follow_redirects=False
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}",
                    self._handle,
                    methods=[
                        "GET",
                        "HEAD",
                        "OPTIONS",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                    ],
                )
            ]
        )

    async def start(self) -> None:
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
            )
        )
        self._task = asyncio.create_task(self._server.serve(), name="github-proxy")
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if self.socket_path.exists():
            self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)
        await self._client.aclose()

    async def _handle(self, request: Request) -> Response:
        host = (request.headers.get("host") or self.api_host).split(":")[0].lower()
        body = await request.body()
        path = request.url.path
        if not allowed(request.method, host, path, body, self.api_host):
            log.info("refused %s %s%s from an agent", request.method, host, path)
            return JSONResponse({"message": REFUSED}, status_code=403)
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP and k.lower() != "authorization"
        }
        if host == self.api_host:
            token = await self._token()
            if token:
                headers["authorization"] = f"token {token}"
        query = f"?{request.url.query}" if request.url.query else ""
        upstream = self._client.build_request(
            request.method,
            f"https://{host}{path}{query}",
            headers=headers,
            content=body or None,
        )
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"message": f"GitHub could not be reached: {exc}"}, status_code=502
            )
        # The body goes back decoded, so its encoding and length headers do not.
        out = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in _HOP and k.lower() != "content-encoding"
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=out,
            background=BackgroundTask(response.aclose),
        )


def gh_config(socket_in_container: str) -> dict[str, Any]:
    """The ``gh`` settings that send its requests through the proxy."""
    return {"http_unix_socket": socket_in_container, "prompt": "disabled"}
