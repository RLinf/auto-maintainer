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

"""The agents' read-only door to the dashboard (design 5.13).

An agent talking with a person reads everything that person may see on the dashboard,
not only the page they have open: its ``am-read`` command (``agents/records_cli.py``)
asks this door, over a Unix socket mounted into its container, for a path of the
dashboard's API, and the door answers it as that person. Each conversation turn gets
a token naming the person (``grant``), revoked when the turn ends. Only ``GET`` of
``/api/...`` passes, never the event stream; the request runs through the dashboard's
own routes and permission checks with the person's session, which the agent never
holds; answers are cut to ``MAX_BYTES`` and stripped of anything that looks like a
credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request as ApiRequest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from ..domain.actions import ActionKind
from ..domain.secrets import redact_secrets
from .api import require_view, service_of
from .auth import SESSION_COOKIE, User, current_user

log = logging.getLogger(__name__)

MAX_BYTES = 400_000
"""The most of one answer an agent gets; the rest is cut with a note."""
TOKEN_SECONDS = 3 * 3600
"""How long a token lives at most, for a turn that never ends."""
CLOSED = ("/api/events",)
"""Paths that never pass: the event stream does not end."""
RECORD_ACTIONS = 40
"""The most recent actions of a PR the chat agent sees, with how each ended."""
RECORD_JOBS = 12
"""The most recent jobs of a PR in its record."""
LOG_LINES = 5000
"""How far back in the service's log ``/api/service/log`` reads."""
LOG_MATCHES = 60


def reading_line(login: str) -> str:
    """What a conversation's prompt says about ``am-read``."""
    return (
        f"You see everything {login} may see on the dashboard, not only the page they "
        "have open: `am-read /api/<path>` prints what that path of the dashboard's API "
        "answers them, read-only (`am-read --index` lists every path; the service "
        "guide names the useful ones). Read what you need before you answer, as often "
        "as you need. The page they have open says where to look first; it is not a "
        "limit."
    )


async def open_door(door: Any, login: str) -> tuple[str | None, dict[str, str]]:
    """A token for ``login`` and the environment that names the door's socket, for
    an agent's task; nothing without a door."""
    if door is None:
        return None, {}
    return await door.grant(login), {"AM_RECORDS_SOCKET": str(door.socket_path)}


class RecordsDoor:
    """Serves ``GET /read?path=/api/...`` on ``socket_path`` for agents.

    Args:
        app: The dashboard, whose API answers.
        socket_path: Where the door listens; mounted into agents' containers.
    """

    def __init__(self, app: Any, socket_path: Path) -> None:
        self.app = app
        self.socket_path = socket_path
        self._tokens: dict[str, tuple[str, float]] = {}
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.door = Starlette(routes=[Route("/read", self._read, methods=["GET"])])

    async def grant(self, login: str) -> str:
        """A token with which an agent reads as ``login``, starting the door first."""
        await self.start()
        now = time.time()
        self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (login, now + TOKEN_SECONDS)
        return token

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)

    def login_of(self, token: str) -> str | None:
        login, expires = self._tokens.get(token, ("", 0.0))
        return login if login and expires > time.time() else None

    async def start(self) -> None:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
            self._server = uvicorn.Server(
                uvicorn.Config(
                    self.door,
                    uds=str(self.socket_path),
                    log_level="warning",
                    lifespan="off",
                    access_log=False,
                )
            )
            self._task = asyncio.create_task(self._server.serve(), name="records")
            for _ in range(100):
                if self._server.started or self._task.done():
                    break
                await asyncio.sleep(0.05)
            if self.socket_path.exists():
                self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        self._tokens.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)
        self._task = None

    async def read(self, token: str, path: str) -> tuple[int, str]:
        """What ``path`` of the dashboard's API answers the token's person: the
        status and the body, cut and redacted."""
        login = self.login_of(token)
        if login is None:
            return 403, "this conversation's token has ended"
        parts = urlsplit(path)
        if (
            parts.scheme
            or parts.netloc
            or not parts.path.startswith("/api/")
            or ".." in parts.path.split("/")
            or parts.path.rstrip("/") in CLOSED
        ):
            return 400, "only paths of the dashboard's API, such as /api/prs, are read"
        cookie = self.app.state.sessions.session_for(User(login=login))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://dashboard",
            timeout=120,
            cookies={SESSION_COOKIE: cookie},
        ) as client:
            try:
                r = await client.get(path)
            except httpx.HTTPError as e:
                return 502, f"the dashboard did not answer: {e}"
        text = r.text
        if len(text) > MAX_BYTES:
            text = (
                text[:MAX_BYTES]
                + f"\n[cut: the answer has {len(r.text)} characters; ask for less, "
                "such as one repository or one PR]"
            )
        return r.status_code, redact_secrets(text)[0]

    async def _read(self, request: Request) -> Response:
        token = request.headers.get("x-records-token", "")
        path = request.query_params.get("path", "")
        if not token:
            return JSONResponse({"error": "no token"}, 403)
        status, text = await self.read(token, path)
        return PlainTextResponse(text, status_code=status)


# -- records the dashboard's pages do not show as such ------------------------------


def build_router() -> APIRouter:
    """Read-only records for agents (and anyone signed in), under the same
    permissions as the dashboard: a PR's record, a repository's failures, and the
    log lines about an id (operators and the owner)."""
    r = APIRouter(prefix="/api")

    @r.get("/prs/{repo:path}/{number:int}/record")
    async def pr_record(
        request: ApiRequest, repo: str, number: int, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Every action the service proposed or took on the PR, oldest first, with how
        each ended, its proposal, a review draft's comments, and the PR's jobs."""
        service = service_of(request)
        await require_view(service, user, repo)
        text = await _service_record(service, repo, number)
        return {"record": redact_secrets(text)[0]}

    @r.get("/repos/{repo:path}/failures")
    async def failures(
        request: ApiRequest, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The repository's latest actions that failed, were refused, or superseded."""
        service = service_of(request)
        await require_view(service, user, repo)
        return {"failures": redact_secrets(await _repo_failures(service, repo))[0]}

    @r.get("/service/log")
    async def log_lines(
        request: ApiRequest, text: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The service's latest log lines that contain ``text``, such as an action or
        job id; for operators and the owner of this service."""
        service = service_of(request)
        if (
            user.login not in service.settings.operators
            and not (await service.service_gate(user.login)).allowed
        ):
            raise HTTPException(
                403, "only operators and the owner of this service read its log"
            )
        if not text.strip():
            raise HTTPException(422, "say what the lines contain")
        path = Path(request.app.state.log_path)
        lines = await asyncio.to_thread(_log_lines, path, text.strip())
        return {"lines": redact_secrets(lines)[0]}

    return r


def _draft_summary(payload: dict[str, Any]) -> str:
    """A review draft in a line: its kind and where each inline comment is, to check
    against the PR's diff."""
    comments = payload.get("comments") or []
    places = [
        f"{c.get('path')}:"
        + (
            f"{c['start_line']}-{c.get('line')}"
            if c.get("start_line")
            else str(c.get("line"))
        )
        + ("" if c.get("side", "RIGHT") == "RIGHT" else " (old side)")
        for c in comments
    ]
    return f"draft: {payload.get('event', 'COMMENT')} review, " + (
        f"{len(places)} inline comments at " + ", ".join(places)
        if places
        else "no inline comments"
    )


async def _service_record(service: Any, repo: str, number: int) -> str:
    """The service's own record of ``repo#number``, oldest first: each action it
    proposed or took, when it last changed, how it ended (status, error, the
    policy's rule), the proposal behind it (who decided, why, its warnings), for a
    review draft its kind and where its inline comments are, and its recent jobs
    (the ``pr_record`` lookup)."""
    actions = await service.store.list_actions(
        repo=repo, number=number, limit=RECORD_ACTIONS
    )
    lines = []
    for record in sorted(actions, key=lambda r: r.action.created_at):
        a = record.action
        parts = [
            f"{a.created_at:%Y-%m-%d %H:%M} UTC {a.kind} "
            f"at {(a.head_sha or '-')[:7]}: {record.status}"
        ]
        if record.decision is not None:
            parts.append(f"policy {record.decision.kind} ({record.decision.rule})")
        proposal = await service.store.proposal_for_action(a.id)
        if proposal is not None:
            parts.append(
                f"proposal {proposal.state}"
                + (f" for {proposal.requested_by}" if proposal.requested_by else "")
                + (f", decided by {proposal.decided_by}" if proposal.decided_by else "")
            )
            if proposal.reason:
                parts.append(f"reason: {proposal.reason}")
            if proposal.warnings:
                parts.append("warnings: " + "; ".join(proposal.warnings))
        if record.post_as:
            parts.append(f"as {record.post_as}")
        if a.kind in (ActionKind.REVIEW_POST, ActionKind.REVIEW_UPDATE):
            parts.append(_draft_summary(a.payload))
        if record.updated_at and record.updated_at > a.created_at:
            parts.append(f"last changed {record.updated_at:%Y-%m-%d %H:%M} UTC")
        if record.error:
            parts.append(f"error: {record.error}")
        lines.append(" | ".join(parts))
    jobs = [j for j in await service.store.list_jobs(repo=repo) if j.number == number]
    for job in sorted(jobs, key=lambda j: j.updated_at)[-RECORD_JOBS:]:
        lines.append(
            f"{job.updated_at:%Y-%m-%d %H:%M} UTC job {job.playbook}: {job.status}"
            + (f" | error: {job.error}" if job.error else "")
        )
    return "\n".join(lines) or "nothing yet"


async def _repo_failures(service: Any, repo: str) -> str:
    """The repository's latest actions that did not run as planned."""
    statuses = ["failed", "denied", "superseded"]
    records = await service.store.list_actions(repo=repo, statuses=statuses, limit=30)
    lines = [
        f"{r.action.created_at:%Y-%m-%d %H:%M} UTC {r.action.kind} "
        f"#{r.action.number}: {r.status}" + (f" | error: {r.error}" if r.error else "")
        for r in sorted(records, key=lambda r: r.action.created_at)
    ]
    return "\n".join(lines) or "nothing failed recently"


def _log_lines(path: Path, needle: str) -> str:
    """The service log's latest lines that contain ``needle``, ignoring case."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-LOG_LINES:]
    except OSError:
        return "the service's log cannot be read"
    low = needle.lower()
    hits = [ln.rstrip() for ln in tail if low in ln.lower()]
    return "\n".join(hits[-LOG_MATCHES:]) or f"no recent line contains {needle!r}"
