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

First run: the service has no ``local.yaml`` (or one from a GitHub App or token
setup, which must connect the GitHub CLI instead) and serves only this. Anyone who
reaches the page may begin, but the setup belongs to the first person who signs in on
the forge as an admin of the service's repository, and someone without the setup
link from the terminal may only do so with the repository this checkout came from.
From then on only that person's browser continues. Later: the same steps change the
host setup from the dashboard, for the deployment's owner (``Service.service_gate``)
or a browser that opened the setup link ``am setup`` prints, through which an admin
of the service's repository takes ownership when its owner cannot. The steps live in
``hostsetup.HostSetup``.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth

SETUP_COOKIE = "am_setup"
SETUP_HOURS = 12

Authorize = Callable[[Request], Awaitable[dict[str, Any]]]
"""Returns what is known about the caller, or raises ``HTTPException``."""


class MemoryBody(BaseModel):
    repo: str | None = None
    create: bool = False


class RepoBody(BaseModel):
    link: str = Field(min_length=1, max_length=500)
    kind: Literal["github", "gitlab"] | None = None
    """For a forge other than github.com and gitlab.com."""
    oauth_client_id: str | None = Field(default=None, max_length=200)
    """A self-managed GitLab's OAuth application, for the one-time code."""


class SignInBody(BaseModel):
    role: Literal["owner", "service"] = "owner"
    token: str = Field(default="", max_length=4000)


class FinishBody(BaseModel):
    base_url: str
    default_agent: str | None = None


def remember_link(
    app: FastAPI, response: Any, data: dict[str, Any] | None = None
) -> None:
    """Mark the browser as one that opened the host's setup link, for
    ``SETUP_HOURS``."""
    response.set_cookie(
        SETUP_COOKIE,
        app.state.sessions.sign(
            {
                **(data or {}),
                "code": True,
                "setup": True,
                "exp": int(time.time()) + SETUP_HOURS * 3600,
            }
        ),
        httponly=True,
        samesite="lax",
        secure=app.state.cookie_secure,
        max_age=SETUP_HOURS * 3600,
    )


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
    ``person_token`` gives the signed-in person's own token on the main forge, with
    which the memory repository is checked and created."""
    r = APIRouter()
    errors = (ValueError, RuntimeError)

    async def may_begin(request: Request) -> dict[str, Any]:
        """Before anyone owns the setup, visitors may try; after, only the owner."""
        who = await visitor(request)
        if first_run and steps().claimer is None:
            return who
        return await owner(request)

    async def may_claim(who: dict[str, Any]) -> bool:
        """Whether signing in as an admin of the service's repository makes the
        caller its owner here: with the setup link, on a host already set up (the
        caller passed ``owner``), or with the repository this checkout came from."""
        return (
            (not first_run) or bool(who.get("code")) or await steps().is_checkout_repo()
        )

    @r.get("/api/setup/status")
    async def status(request: Request) -> dict[str, Any]:
        who = await visitor(request)
        s = steps()
        mine = (
            (not first_run)
            or bool(who.get("code"))
            or (s.claimer is not None and who.get("by") == s.claimer["login"])
        )
        if not mine and s.claimer is not None:
            return {"first_run": first_run, "busy": s.claimer["login"], "mine": False}
        private = bool(who.get("code")) or not first_run or s.claimer is not None
        return {
            "first_run": first_run,
            "mine": mine,
            "code": bool(who.get("code")),
            "may_claim": await may_claim(who),
            **s.summary(),
            "detected": await s.detect(private=private),
        }

    @r.post("/api/setup/repo")
    async def repo(request: Request, body: RepoBody) -> dict[str, Any]:
        """Name the service's repository by its link; on a new host, its forge
        becomes the main forge."""
        await may_begin(request)
        try:
            return await steps().set_repo(body.link, body.kind, body.oauth_client_id)
        except errors as e:
            raise _error(e) from e

    @r.post("/api/setup/cli/install")
    async def cli_install(request: Request) -> dict[str, Any]:
        """Install the main forge's CLI into the service's data folder."""
        await may_begin(request)
        try:
            return await steps().install_cli()
        except errors as e:
            raise _error(e) from e

    async def _may_sign_in(request: Request, role: str) -> dict[str, Any]:
        return await (may_begin(request) if role == "owner" else owner(request))

    def _trusted(who: dict[str, Any]) -> bool:
        """The setup link, or the owner of a host set up already."""
        return bool(who.get("code")) or not first_run

    def _claimed(request: Request, result: dict[str, Any]) -> Any:
        response = JSONResponse(result)
        s = steps()
        if first_run and result.get("state") == "done" and s.claimer:
            claimed(request, response, s.claimer)
        return response

    @r.post("/api/setup/signin/start")
    async def signin_start(request: Request, body: SignInBody) -> dict[str, Any]:
        """Start the forge CLI's one-time code; the page shows it."""
        await _may_sign_in(request, body.role)
        try:
            return await steps().signin_start(body.role)
        except errors as e:
            raise _error(e) from e

    @r.post("/api/setup/signin/poll")
    async def signin_poll(request: Request) -> Any:
        s = steps()
        who = await _may_sign_in(request, s.signing or "owner")
        return _claimed(
            request,
            await s.signin_poll(may_claim=await may_claim(who), trusted=_trusted(who)),
        )

    @r.post("/api/setup/signin/token")
    async def signin_token(request: Request, body: SignInBody) -> Any:
        """Sign in with a personal access token (GitLab)."""
        who = await _may_sign_in(request, body.role)
        if not body.token.strip():
            raise HTTPException(422, "paste a personal access token")
        try:
            result = await steps().signin_token(
                body.role,
                body.token,
                may_claim=await may_claim(who),
                trusted=_trusted(who),
            )
        except errors as e:
            raise _error(e) from e
        return _claimed(request, result)

    @r.post("/api/setup/host-login")
    async def host_login(request: Request) -> Any:
        """Act as the forge CLI login already on the host."""
        who = await owner(request)
        if first_run and not who.get("code"):
            raise HTTPException(
                403,
                "using the host's existing CLI login needs the setup link from the "
                "terminal; sign in on the forge instead",
            )
        try:
            return JSONResponse(await steps().use_host_login())
        except errors as e:
            raise _error(e) from e

    @r.post("/api/setup/check")
    async def check(request: Request) -> dict[str, Any]:
        """Check the service's repository again."""
        await owner(request)
        s = steps()
        try:
            s.checks = await s.verify()
        except errors as e:
            raise _error(e) from e
        return {"checks": s.checks, "repo": s.cfg.code_repo}

    @r.post("/api/setup/copy")
    async def copy(request: Request) -> dict[str, Any]:
        """Make the owner's own copy of the repository this host came from."""
        await owner(request)
        try:
            return await steps().copy_upstream()
        except errors as e:
            raise _error(e) from e

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
        who = await owner(request)
        s = steps()
        if not first_run and who.get("code") and s.claimer is None:
            # The setup link alone changes nothing: an admin of the service's
            # repository signs in first, and becomes its owner.
            raise HTTPException(
                403,
                f"sign in as an admin of {s.cfg.code_repo or 'the service repository'} "
                "first; they become the owner of this service",
            )
        try:
            cfg = s.finish(body.base_url, body.default_agent)
            await on_finish(cfg)
        except errors as e:
            raise _error(e) from e
        response = JSONResponse({"redirect": "/"})
        person = s.claimer
        if first_run and person:
            # The owner proved who they are on the forge: sign them in.
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
    ca_cert: Path | None = None,
) -> FastAPI:
    """The app ``am serve`` runs before the host is set up: the setup page only, and
    the private authority's certificate (``app.install_authority``)."""
    from .app import install_authority, install_csrf, install_spa

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
    install_authority(app, ca_cert)

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
            401, "sign in on the forge as an admin of the service's repository first"
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
