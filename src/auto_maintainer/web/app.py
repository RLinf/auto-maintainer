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

"""The FastAPI application: API, auth, chat, webhooks, health, and the built dashboard."""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from ..domain.forges import check_repo, default_forge
from ..service import Service
from . import (
    api,
    auth,
    chat,
    forge_setup,
    governance,
    jobs,
    my_agents,
    ownership,
    people_talk,
    review_talk,
    settings,
    setup,
    translate,
)
from .dashboard import STATIC, missing_page


@dataclass(frozen=True)
class WebhookCheck:
    """How one forge's webhook deliveries are checked and read (its ``ForgeKind``)."""

    verify: Callable[[bytes, bytes, str | None], bool]
    target: Callable[[str, dict[str, Any]], tuple[str, int | None] | None]
    secret_env: str = "AM_WEBHOOK_SECRET"
    signature_header: str = "x-hub-signature-256"
    event_header: str = "x-github-event"


def create_app(
    service: Service,
    *,
    data_dir: Path,
    api_url: str = "https://api.github.com",
    webhook_secret_env: str = "AM_WEBHOOK_SECRET",
    cookie_secure: bool = False,
    verify_webhook: Callable[[bytes, bytes, str | None], bool] | None = None,
    webhook_target: Callable[[str, dict[str, Any]], tuple[str, int | None] | None]
    | None = None,
    allow_dev_login: bool = False,
    links_path: Path | None = None,
    gh_signin: Callable[[str], Any] | None = None,
    forge_signins: Mapping[str, auth.ForgeSignIn] | None = None,
    webhooks: Mapping[str, WebhookCheck] | None = None,
    setup_steps: Callable[[], Any] | None = None,
    on_host_change: Callable[[Any], Awaitable[None]] | None = None,
    forge_steps: Any | None = None,
    log_path: Path | None = None,
    ca_cert: Path | None = None,
) -> FastAPI:
    """Build the app. ``verify_webhook`` and ``webhook_target`` come from the forge adapter.

    ``allow_dev_login`` enables ``/auth/dev``, which signs anyone in as any login
    without a password. Only the demo, which runs on fakes, turns it on; an instance
    that talks to a real forge, including one in dev mode, never does.

    ``links_path`` enables sign-in with one-time links from ``am login``;
    ``gh_signin`` (a factory of ``hostsetup.GhLogin`` by flow id) enables signing in
    on the web by authorizing the GitHub CLI, the only way to sign in with GitHub;
    while ``gh`` is not on the server's ``PATH``, the sign-in page offers to install it.
    ``setup_steps`` and ``on_host_change`` let the owner of the service's repository
    change the host setup from the dashboard (``web/setup.py``), and with
    ``forge_steps`` (``hostsetup.ForgeSteps``) add a forge (``web/forge_setup.py``).
    ``ca_cert`` is the private authority's certificate when the dashboard serves
    HTTPS with one (``install_authority``).
    """
    app = FastAPI(
        title="auto-maintainer", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.service = service
    app.state.data_dir = data_dir
    app.state.log_path = log_path or data_dir / "logs" / "service.log"
    """The log ``am start`` keeps, which the Service page shows to its maintainers."""
    app.state.sessions = auth.Sessions(data_dir / ".session_secret")
    app.state.cookie_secure = cookie_secure

    install_csrf(app)

    app.include_router(
        auth.router(api_url, allow_dev_login, links_path, gh_signin, forge_signins)
    )
    checks = dict(webhooks or {})
    if verify_webhook is not None and webhook_target is not None:
        checks.setdefault(
            "github",
            WebhookCheck(verify_webhook, webhook_target, webhook_secret_env),
        )
    app.include_router(api.build_router())
    app.include_router(chat.build_router())
    app.include_router(settings.build_router())
    app.include_router(jobs.build_router())
    app.include_router(review_talk.build_router())
    app.include_router(governance.build_router())
    app.include_router(people_talk.build_router())
    app.include_router(translate.build_router())
    app.include_router(my_agents.build_router())
    if setup_steps is not None and on_host_change is not None:

        async def maintainer(request: Request) -> dict[str, Any]:
            user = auth.current_user(request)
            decision = await service.service_gate(user.login)
            if not decision.allowed:
                raise HTTPException(403, decision.reason)
            return {"by": user.login}

        def setup_link(request: Request) -> bool:
            """Whether this browser opened the setup link ``am setup`` printed."""
            data = app.state.sessions.verify(request.cookies.get(setup.SETUP_COOKIE))
            return bool(data and data.get("code"))

        async def owner_or_link(request: Request) -> dict[str, Any]:
            """The owner, or whoever holds the host's setup link: through it an admin
            of the service's repository takes ownership when its owner cannot."""
            if setup_link(request):
                return {"code": True}
            return await maintainer(request)

        async def own_token(request: Request) -> str | None:
            try:
                user = auth.current_user(request)
            except HTTPException:
                return None
            tokens = service.a.user_tokens
            kept = await tokens.get(user.login) if tokens is not None else None
            return kept.token if kept else None

        @app.get("/setup/start")
        async def setup_start(code: str) -> Any:
            if not setup_steps().check_code(code):
                return RedirectResponse("/setup?error=code", status_code=303)
            response = RedirectResponse("/setup", status_code=303)
            setup.remember_link(app, response)
            return response

        if forge_steps is not None:
            app.include_router(
                forge_setup.build_router(forge_steps, maintainer, on_host_change)
            )
            app.include_router(ownership.build_router(forge_steps, on_host_change))
        app.include_router(
            setup.build_router(
                setup_steps,
                owner_or_link,
                owner_or_link,
                on_host_change,
                first_run=False,
                person_token=own_token,
            )
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "login": service.settings.login,
            "dev_mode": service.settings.dev_mode,
        }

    install_authority(app, ca_cert)

    @app.post("/webhooks/{forge}")
    async def webhook(request: Request, forge: str) -> dict[str, Any]:
        """A forge's delivery: checked as its kind checks them, it starts a poll of
        the repository it names, which is trusted for nothing else."""
        check = checks.get(forge)
        if check is None:
            raise HTTPException(404, "webhooks are not enabled")
        secret = os.environ.get(check.secret_env)
        body = await request.body()
        if not secret or not check.verify(
            secret.encode(), body, request.headers.get(check.signature_header)
        ):
            raise HTTPException(401, "bad signature")
        target = check.target(
            request.headers.get(check.event_header, ""), json.loads(body or b"{}")
        )
        if target is None:
            return {"ignored": True}
        path, _number = target
        try:
            repo = check_repo(path if forge == default_forge() else f"{forge}:{path}")
        except ValueError:
            return {"ignored": True}
        record = await service.store.get_repo(repo)
        if record is None or not record.active:
            return {"ignored": True}
        events = await service.poller.poll_repo(repo)
        return {"events": len(events)}

    install_spa(app)
    return app


def install_authority(app: FastAPI, ca_cert: Path | None) -> None:
    """Offer the private authority's certificate at ``/tls/ca.crt`` to everyone, when
    the server has one: it is public, and people install it to trust the dashboard."""
    if ca_cert is None:
        return

    @app.get("/tls/ca.crt", include_in_schema=False)
    async def authority() -> Response:
        if not ca_cert.is_file():
            raise HTTPException(404, "no authority certificate on this host")
        return FileResponse(
            ca_cert,
            media_type="application/x-x509-ca-cert",
            filename="auto-maintainer-ca.crt",
        )


def install_csrf(app: FastAPI) -> None:
    """Every state-changing ``/api`` call must echo the ``am_csrf`` cookie in a header.
    A signed-in browser that lost the cookie gets a new one with its next read, so a
    reload is enough to make changes again."""

    @app.middleware("http")
    async def csrf(request: Request, call_next: Any) -> Response:
        safe = request.method in ("GET", "HEAD", "OPTIONS")
        if not safe and request.url.path.startswith("/api/"):
            cookie = request.cookies.get(auth.CSRF_COOKIE, "")
            header = request.headers.get("x-am-csrf", "")
            if not cookie or not hmac.compare_digest(cookie, header):
                return JSONResponse(
                    {"detail": "missing or wrong CSRF token"}, status_code=403
                )
        response = await call_next(request)
        if (
            safe
            and not request.cookies.get(auth.CSRF_COOKIE)
            and request.app.state.sessions.verify(
                request.cookies.get(auth.SESSION_COOKIE)
            )
            and not any(
                value.startswith(f"{auth.CSRF_COOKIE}=")
                for value in response.headers.getlist("set-cookie")
            )
        ):
            auth.issue_csrf(request, response)
        return response


def install_spa(app: FastAPI) -> None:
    """Serve the built dashboard: its files, and ``index.html`` for every page route.

    ``index.html`` is sent with ``Cache-Control: no-cache``, so a browser asks again
    after every restart or deploy and loads the new build; the files under
    ``/assets`` carry a content hash in their names and may be cached.
    """
    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith(("api/", "auth/")):
            raise HTTPException(404, "not found")
        if not (STATIC / "index.html").is_file():
            return HTMLResponse(missing_page(), status_code=503)
        candidate = STATIC / path
        if path and candidate.is_file() and STATIC in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(
            STATIC / "index.html", headers={"Cache-Control": "no-cache"}
        )
