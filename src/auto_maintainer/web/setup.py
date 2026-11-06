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

"""The setup page's API (design 7.3), in two modes.

First run: the service has no ``local.yaml`` and serves only this. Anyone who reaches
the page may begin, but the setup belongs to the first person who proves through
GitHub that they maintain the service's own repository: by authorizing the GitHub CLI
login, by signing in through the GitHub App the page created, or with the token they
paste. From then on only that person's browser continues. The one-time code from the
terminal is a fallback that also proves ownership. Later: the same steps change the
host setup from the dashboard, and every call needs a maintainer of the service's
repository (``Service.service_gate``). The steps live in ``hostsetup.HostSetup``.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import auth

SETUP_COOKIE = "am_setup"
SETUP_HOURS = 12

Authorize = Callable[[Request], Awaitable[dict[str, Any]]]
"""Returns what is known about the caller, or raises ``HTTPException``."""


class GitHubBody(BaseModel):
    mode: Literal["gh", "token"]
    token: str = ""


class AppBody(BaseModel):
    purpose: Literal["identity", "signin"]
    base_url: str
    org: str | None = None


class MemoryBody(BaseModel):
    repo: str | None = None
    create: bool = False


class FinishBody(BaseModel):
    base_url: str
    code_repo: str | None = None


def _error(e: Exception) -> HTTPException:
    return HTTPException(422, str(e))


def build_router(
    steps: Callable[[], Any],
    visitor: Authorize,
    owner: Authorize,
    on_finish: Callable[[Any], Awaitable[None]],
    first_run: bool,
    claimed: Callable[[Request, Any, dict[str, Any]], None] = lambda *_: None,
    person_token: Callable[[Request], Awaitable[str | None]] | None = None,
) -> APIRouter:
    """``visitor`` admits whoever may begin; ``owner`` whoever owns the setup;
    ``claimed`` marks the caller's browser as the owner's once they proved it;
    ``person_token`` gives the signed-in maintainer's own GitHub token, with which
    the memory repository is checked and created."""
    r = APIRouter()
    errors = (ValueError, RuntimeError)

    async def may_begin(request: Request) -> dict[str, Any]:
        """Before anyone owns the setup, visitors may try; after, only the owner."""
        who = await visitor(request)
        if first_run and steps().claimer is None:
            return who
        return await owner(request)

    @r.get("/api/setup/status")
    async def status(request: Request) -> dict[str, Any]:
        who = await visitor(request)
        s = steps()
        mine = (
            (not first_run)
            or bool(who.get("code"))
            or (s.claimer is not None and who.get("by") == s.claimer["login"])
        )
        summary = s.summary()
        if not mine and s.claimer is not None:
            return {"first_run": first_run, "busy": s.claimer["login"], "mine": False}
        return {
            "first_run": first_run,
            "mine": mine,
            "code": bool(who.get("code")),
            **summary,
            "detected": await s.detect(private=bool(who.get("code")) or not first_run),
        }

    @r.post("/api/setup/github")
    async def github(request: Request, body: GitHubBody) -> Any:
        who = await may_begin(request)
        s = steps()
        try:
            if body.mode == "gh":
                if first_run and not who.get("code"):
                    raise HTTPException(
                        403,
                        "using the host's existing GitHub CLI login needs the setup link "
                        "from the terminal; sign in with GitHub instead",
                    )
                identity = await s.use_gh()
            else:
                identity = await s.use_token(body.token, claim=first_run)
        except errors as e:
            raise _error(e) from e
        response = JSONResponse(identity)
        if first_run and s.claimer:
            claimed(request, response, s.claimer)
        return response

    @r.post("/api/setup/gh/start")
    async def gh_start(request: Request) -> dict[str, Any]:
        """Start a GitHub CLI login; the page shows the one-time code."""
        await may_begin(request)
        try:
            return await steps().gh_web_start()
        except errors as e:
            raise _error(e) from e

    @r.post("/api/setup/gh/poll")
    async def gh_poll(request: Request) -> Any:
        await may_begin(request)
        s = steps()
        result = await s.gh_web_poll(claim=first_run)
        response = JSONResponse(result)
        if first_run and result.get("state") == "done" and s.claimer:
            claimed(request, response, s.claimer)
        return response

    @r.post("/api/setup/app")
    async def app(request: Request, body: AppBody) -> dict[str, Any]:
        """The manifest the browser posts to GitHub to create a GitHub App."""
        await may_begin(request)
        return steps().app_manifest(body.base_url, body.purpose, body.org or None)

    @r.get("/setup/app/callback")
    async def app_callback(request: Request, code: str, state: str) -> Any:
        await may_begin(request)
        try:
            result = await steps().complete_app(code, state)
        except errors as e:
            return RedirectResponse(
                f"/setup?app_error={quote(str(e))}", status_code=303
            )
        return RedirectResponse(f"/setup?app={result['purpose']}", status_code=303)

    @r.get("/setup/signin")
    async def signin(request: Request) -> Any:
        """Sign in through the app this setup created, to prove who you are."""
        await may_begin(request)
        state = secrets.token_urlsafe(16)
        redirect = f"{steps().cfg.base_url}/setup/signin/callback"
        try:
            url = steps().signin_url(redirect, state)
        except errors as e:
            return RedirectResponse(
                f"/setup?app_error={quote(str(e))}", status_code=303
            )
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            "am_oauth_state", state, httponly=True, samesite="lax", max_age=600
        )
        return response

    @r.get("/setup/signin/callback")
    async def signin_callback(request: Request, code: str, state: str) -> Any:
        await may_begin(request)
        if not secrets.compare_digest(state, request.cookies.get("am_oauth_state", "")):
            return RedirectResponse("/setup?app_error=try+again", status_code=303)
        s = steps()
        redirect = f"{s.cfg.base_url}/setup/signin/callback"
        try:
            token = await s.signin_token(code, redirect)
            person = await (s.claim(token) if first_run else s._user(token))
        except errors as e:
            return RedirectResponse(
                f"/setup?app_error={quote(str(e))}", status_code=303
            )
        response = RedirectResponse("/setup?signed_in=1", status_code=303)
        if first_run:
            claimed(request, response, person)
        return response

    @r.post("/api/setup/memory")
    async def memory(request: Request, body: MemoryBody) -> dict[str, Any]:
        await owner(request)
        token = await person_token(request) if person_token else None
        try:
            return await steps().memory(body.repo or None, body.create, token)
        except errors as e:
            raise _error(e) from e

    @r.post("/api/setup/finish")
    async def finish(request: Request, body: FinishBody) -> Any:
        await owner(request)
        s = steps()
        try:
            cfg = s.finish(body.base_url, body.code_repo or None)
            await on_finish(cfg)
        except errors as e:
            raise _error(e) from e
        response = JSONResponse({"redirect": "/"})
        person = s.claimer or (
            s.identity
            if s.identity and s.identity.get("kind") in ("gh", "token")
            else None
        )
        if first_run and person:
            # The owner proved who they are through GitHub (or held the code at the
            # host while the host acts as this account): sign them in.
            auth.set_session(
                request,
                response,
                auth.User(
                    login=person["login"],
                    name=person.get("name") or person["login"],
                    avatar_url=person.get("avatar_url") or "",
                ),
            )
        response.delete_cookie(SETUP_COOKIE)
        return response

    return r


def create_setup_app(
    steps: Any,
    *,
    data_dir: Path,
    on_finish: Callable[[Any], Awaitable[None]],
    cookie_secure: bool = False,
) -> FastAPI:
    """The app ``am serve`` runs before the host is set up: the setup page only."""
    from .app import install_csrf, install_spa

    app = FastAPI(title="auto-maintainer setup", docs_url=None, openapi_url=None)
    app.state.sessions = auth.Sessions(data_dir / ".session_secret")
    app.state.cookie_secure = cookie_secure

    def read(request: Request) -> dict[str, Any]:
        return app.state.sessions.verify(request.cookies.get(SETUP_COOKIE)) or {}

    def write(response: Any, data: dict[str, Any]) -> None:
        response.set_cookie(
            SETUP_COOKIE,
            app.state.sessions.sign(
                {**data, "setup": True, "exp": int(time.time()) + SETUP_HOURS * 3600}
            ),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=SETUP_HOURS * 3600,
        )

    @app.middleware("http")
    async def csrf_cookie(request: Request, call_next: Any) -> Any:
        """Give every browser a CSRF token before its first change."""
        response = await call_next(request)
        if not request.cookies.get(auth.CSRF_COOKIE):
            response.set_cookie(
                auth.CSRF_COOKIE,
                secrets.token_urlsafe(24),
                samesite="strict",
                secure=cookie_secure,
            )
        return response

    install_csrf(app)

    async def visitor(request: Request) -> dict[str, Any]:
        return read(request)

    async def owner(request: Request) -> dict[str, Any]:
        who = read(request)
        claimer = steps.claimer
        if who.get("code") or (claimer and who.get("by") == claimer["login"]):
            return who
        if claimer:
            raise HTTPException(403, f"{claimer['login']} is setting this host up")
        raise HTTPException(
            401, "sign in with GitHub as a maintainer of the service's repository first"
        )

    def claimed(request: Request, response: Any, person: dict[str, Any]) -> None:
        write(response, {**read(request), "by": person["login"]})

    @app.get("/setup/start")
    async def start(request: Request, code: str) -> Any:
        if not steps.check_code(code):
            return RedirectResponse("/setup?error=code", status_code=303)
        response = RedirectResponse("/setup", status_code=303)
        write(response, {**read(request), "code": True})
        return response

    @app.get("/api/me")
    async def me() -> Any:
        return JSONResponse({"detail": "setup required"}, status_code=503)

    @app.get("/auth/config")
    async def auth_config() -> dict[str, bool]:
        return {"setup": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "setup": True}

    app.include_router(
        build_router(lambda: steps, visitor, owner, on_finish, True, claimed)
    )
    install_spa(app)
    return app
