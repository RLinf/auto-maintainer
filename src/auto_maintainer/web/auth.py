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

"""Dashboard sign-in (design 5.13): authorizing the GitHub CLI with a one-time code,
one-time links from ``am login``, and HMAC-signed session cookies.

The token a sign-in yields tells who the person is, and is kept under their login
only, to do what they approve as themselves (``remember_token``); sign-out forgets
it. Repository permissions come from the service's own forge reader.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..domain.forges import person_on

SESSION_COOKIE = "am_session"
CSRF_COOKIE = "am_csrf"
SESSION_DAYS = 7


class User(BaseModel):
    login: str
    name: str = ""
    avatar_url: str = ""


class Sessions:
    def __init__(self, secret_path: Path) -> None:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
            os.chmod(secret_path, 0o600)
        self._key = secret_path.read_text(encoding="utf-8").strip().encode()

    def sign(self, data: dict[str, Any]) -> str:
        raw = base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode()
        sig = hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def verify(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        expected = hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()))
        except ValueError:
            return None
        return data if data.get("exp", 0) > time.time() else None

    def session_for(self, user: User) -> str:
        return self.sign(
            {**user.model_dump(), "exp": int(time.time()) + SESSION_DAYS * 86400}
        )


def current_user(request: Request) -> User:
    sessions: Sessions = request.app.state.sessions
    data = sessions.verify(request.cookies.get(SESSION_COOKIE))
    if data is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return User(
        login=data["login"],
        name=data.get("name", ""),
        avatar_url=data.get("avatar_url", ""),
    )


def set_session(request: Request, response: Any, user: User) -> Any:
    """Sign ``user`` in on ``response``: the session cookie and a fresh CSRF token."""
    sessions: Sessions = request.app.state.sessions
    secure = request.app.state.cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        sessions.session_for(user),
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_DAYS * 86400,
    )
    issue_csrf(request, response)
    return response


def issue_csrf(request: Request, response: Any) -> None:
    """A fresh CSRF token for the dashboard to echo on every change (``am_csrf``,
    readable by the page). It lives as long as the session: a token that lived only
    for the browser's session was lost whenever a phone closed its browser, while the
    person stayed signed in, and every change they made was then refused."""
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(24),
        httponly=False,
        samesite="strict",
        secure=request.app.state.cookie_secure,
        max_age=SESSION_DAYS * 86400,
    )


async def remember_token(
    request: Request,
    login: str,
    token: str,
    source: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> None:
    """Keep the person's GitHub token on the host, so reviews they approve can be
    posted as them. Signing out deletes it."""
    service = getattr(request.app.state, "service", None)
    store = getattr(getattr(service, "a", None), "user_tokens", None)
    if store is None:
        return
    from ..domain.settings import UserToken

    now = datetime.now(UTC)
    await store.set(
        login,
        UserToken(
            token=token,
            refresh_token=refresh_token,
            expires_at=now + timedelta(seconds=expires_in) if expires_in else None,
            source=source,
            updated_at=now,
        ),
    )


def _login_response(request: Request, user: User, redirect: str | None = "/") -> Any:
    response: Any = (
        RedirectResponse(redirect, status_code=303)
        if redirect
        else JSONResponse({"login": user.login})
    )
    return set_session(request, response, user)


LINK_MINUTES = 10


def create_link(path: Path, login: str, base_url: str) -> str:
    """A one-time sign-in link for ``login``, valid for ``LINK_MINUTES`` (``am login``).

    The link is stored in ``path`` (mode 0600) on the host; whoever runs this on the
    host vouches for the login.
    """
    token = secrets.token_urlsafe(32)
    links = _read_links(path)
    now = time.time()
    links = {k: v for k, v in links.items() if v["exp"] > now}
    links[hashlib.sha256(token.encode()).hexdigest()] = {
        "login": login,
        "exp": now + LINK_MINUTES * 60,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(links, f)
    os.replace(tmp, path)
    return f"{base_url.rstrip('/')}/auth/link?token={token}"


def _read_links(path: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def use_link(path: Path, token: str) -> str | None:
    """The login a link signs in, once; ``None`` for an unknown, used, or old link."""
    links = _read_links(path)
    entry = links.pop(hashlib.sha256(token.encode()).hexdigest(), None)
    if entry is None:
        return None
    path.write_text(json.dumps(links), encoding="utf-8")
    return entry["login"] if entry["exp"] > time.time() else None


async def _github_user(api_url: str, token: str) -> User:
    async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
        r = await client.get(
            f"{api_url}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        data = r.json()
    return User(
        login=data["login"],
        name=data.get("name") or data["login"],
        avatar_url=data.get("avatar_url") or "",
    )


@dataclass(frozen=True)
class ForgeSignIn:
    """Signing in with an account on one more forge (design 5.16): its CLI's device
    flow, or a personal access token pasted once."""

    forge: str
    name: str
    who: Callable[[str], Awaitable[dict[str, str]]]
    """A token to its person: ``login`` as the forge spells it, ``name``,
    ``avatar_url``; raises when the token does not work."""
    device: Callable[[str], Any] | None = None
    """A flow id to a CLI login (``start``, ``state``, ``token``, ``discard``)."""


def router(
    api_url: str,
    dev: bool,
    links_path: Path | None = None,
    gh_signin: Any = None,
    forges: Mapping[str, ForgeSignIn] | None = None,
) -> APIRouter:
    """Sign-in routes: the GitHub CLI's one-time code (``gh_signin``), one-time links
    from ``am login`` (``links_path``), and, in the demo only, ``/auth/dev``."""
    r = APIRouter()
    gh_flows: dict[str, dict[str, Any]] = {}

    def gh_missing() -> bool:
        """The GitHub CLI sign-in is set up, but ``gh`` is not on the server's
        ``PATH``. Looked up on every call, so installing it needs no restart."""
        return gh_signin is not None and shutil.which("gh") is None

    @r.post("/auth/gh/start")
    async def gh_start() -> dict[str, Any]:
        """Sign in by authorizing the GitHub CLI on github.com with a one-time code.

        The CLI's token can do what the person can do on every repository they can
        reach. Each sign-in runs in a folder of its own, never the host's ``gh``
        configuration; the service keeps the token on the host, under the person's
        login only, to do what they approve as themselves, deletes the CLI's files,
        and forgets the token when they sign out."""
        if gh_signin is None or gh_missing():
            raise HTTPException(404, "this host has no GitHub CLI for signing in")
        now = time.time()
        for fid, flow in list(gh_flows.items()):
            if flow["exp"] < now:
                flow["login"].discard()
                gh_flows.pop(fid, None)
        if len(gh_flows) >= 10:
            raise HTTPException(429, "too many sign-ins in progress; try again shortly")
        flow_id = secrets.token_urlsafe(12)
        login = gh_signin(flow_id)
        try:
            started = await login.start()
        except ValueError as e:
            login.discard()
            raise HTTPException(503, str(e)) from e
        gh_flows[flow_id] = {"login": login, "exp": now + 900}
        return {"flow_id": flow_id, **started}

    @r.post("/auth/gh/poll")
    async def gh_poll(request: Request, body: dict[str, str]) -> Any:
        flow = gh_flows.get(body.get("flow_id", ""))
        if flow is None:
            raise HTTPException(404, "unknown or expired sign-in")
        login = flow["login"]
        state = login.state()
        if state == "pending":
            return JSONResponse({"pending": True}, status_code=202)
        token = await login.token() if state == "done" else None
        login.discard()
        gh_flows.pop(body.get("flow_id", ""), None)
        if not token:
            raise HTTPException(401, "GitHub did not complete the sign-in; try again")
        user = await _github_user(api_url, token)
        await remember_token(request, user.login, token, "gh")
        return _login_response(request, user, redirect=None)

    @r.get("/auth/link")
    async def link(request: Request, token: str) -> Any:
        """Sign in with a one-time link from ``am login`` on the host."""
        login = use_link(links_path, token) if links_path else None
        if login is None:
            return RedirectResponse("/?signin=expired", status_code=303)
        return _login_response(request, User(login=login, name=login))

    @r.post("/auth/gh/install")
    async def gh_install(request: Request) -> dict[str, Any]:
        """Install the GitHub CLI when the host has none, so people can sign in.

        Open before sign-in, since nobody can sign in without it; it only ever
        downloads the GitHub CLI's own release into the data folder and checks it
        (``ghcli.install``), and does nothing once ``gh`` is there."""
        if gh_signin is None:
            raise HTTPException(404, "this instance does not sign in through gh")
        from .. import ghcli

        try:
            path = await ghcli.install(request.app.state.data_dir)
        except ghcli.InstallError as e:
            raise HTTPException(502, str(e)) from e
        return {"installed": True, "path": path}

    other = dict(forges or {})
    forge_flows: dict[str, dict[str, Any]] = {}

    def _forge(forge: str) -> ForgeSignIn:
        found = other.get(forge)
        if found is None:
            raise HTTPException(404, f"no sign-in with forge {forge!r} here")
        return found

    async def _sign_in(
        request: Request, f: ForgeSignIn, token: str, source: str
    ) -> Any:
        try:
            who = await f.who(token)
        except Exception as e:
            raise HTTPException(401, f"{f.name} did not accept the token") from e
        login = person_on(f.forge, who["login"])
        await remember_token(request, login, token, source)
        user = User(
            login=login,
            name=who.get("name") or login,
            avatar_url=who.get("avatar_url", ""),
        )
        return _login_response(request, user, redirect=None)

    @r.post("/auth/forges/{forge}/start")
    async def forge_start(forge: str) -> dict[str, Any]:
        """Sign in on another forge with its CLI's one-time code (device flow)."""
        f = _forge(forge)
        if f.device is None:
            raise HTTPException(404, f"{f.name} takes a token here, not a device code")
        now = time.time()
        for fid, flow in list(forge_flows.items()):
            if flow["exp"] < now:
                flow["login"].discard()
                forge_flows.pop(fid, None)
        if len(forge_flows) >= 10:
            raise HTTPException(429, "too many sign-ins in progress; try again shortly")
        flow_id = secrets.token_urlsafe(12)
        login = f.device(flow_id)
        try:
            started = await login.start()
        except ValueError as e:
            login.discard()
            raise HTTPException(503, str(e)) from e
        forge_flows[flow_id] = {"login": login, "exp": now + 900, "forge": forge}
        return {"flow_id": flow_id, **started}

    @r.post("/auth/forges/{forge}/poll")
    async def forge_poll(request: Request, forge: str, body: dict[str, str]) -> Any:
        f = _forge(forge)
        flow = forge_flows.get(body.get("flow_id", ""))
        if flow is None or flow["forge"] != forge:
            raise HTTPException(404, "unknown or expired sign-in")
        login = flow["login"]
        state = login.state()
        if state == "pending":
            return JSONResponse({"pending": True}, status_code=202)
        token = await login.token() if state == "done" else None
        login.discard()
        forge_flows.pop(body.get("flow_id", ""), None)
        if not token:
            raise HTTPException(
                401, f"{f.name} did not complete the sign-in; try again"
            )
        return await _sign_in(request, f, token, "device")

    @r.post("/auth/forges/{forge}/token")
    async def forge_token(request: Request, forge: str, body: dict[str, str]) -> Any:
        """Sign in on another forge with a personal access token, which the service
        keeps as it keeps a device sign-in's token."""
        token = (body.get("token") or "").strip()
        if not token or len(token) > 4000:
            raise HTTPException(422, "paste a personal access token")
        return await _sign_in(request, _forge(forge), token, "token")

    @r.get("/auth/config")
    async def auth_config() -> dict[str, Any]:
        """Which sign-in methods this instance offers."""
        return {
            "dev_login": dev,
            "links": links_path is not None,
            "gh": gh_signin is not None and not gh_missing(),
            "gh_missing": gh_missing(),
            "forges": [
                {"id": f.forge, "name": f.name, "device": f.device is not None}
                for f in other.values()
            ],
        }

    @r.post("/auth/logout")
    async def logout(request: Request) -> Any:
        """Sign out and forget the GitHub token kept for posting as this person."""
        data = request.app.state.sessions.verify(request.cookies.get(SESSION_COOKIE))
        service = getattr(request.app.state, "service", None)
        store = getattr(getattr(service, "a", None), "user_tokens", None)
        if data and store is not None:
            await store.delete(data["login"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    if dev:

        @r.get("/auth/dev")
        async def dev_login(request: Request, login: str) -> Any:
            """Development only: sign in as any login. The instance is forced into shadow mode."""
            return _login_response(request, User(login=login, name=login))

    return r
