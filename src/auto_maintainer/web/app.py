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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..service import Service
from . import api, auth, chat

STATIC = Path(__file__).parent / "static"


def create_app(
    service: Service,
    *,
    data_dir: Path,
    base_url: str = "http://127.0.0.1:7777",
    web_url: str = "https://github.com",
    api_url: str = "https://api.github.com",
    oauth_client_id: str | None = None,
    oauth_secret_env: str = "AM_OAUTH_CLIENT_SECRET",
    webhook_secret_env: str = "AM_WEBHOOK_SECRET",
    cookie_secure: bool = False,
    verify_webhook: Callable[[bytes, bytes, str | None], bool] | None = None,
    webhook_target: Callable[[str, dict[str, Any]], tuple[str, int | None] | None]
    | None = None,
    allow_dev_login: bool = False,
) -> FastAPI:
    """Build the app. ``verify_webhook`` and ``webhook_target`` come from the forge adapter.

    ``allow_dev_login`` enables ``/auth/dev``, which signs anyone in as any login
    without a password. Only the demo, which runs on fakes, turns it on; an instance
    that talks to a real forge, including one in dev mode, never does.
    """
    app = FastAPI(
        title="auto-maintainer", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.service = service
    app.state.sessions = auth.Sessions(data_dir / ".session_secret")
    app.state.cookie_secure = cookie_secure

    @app.middleware("http")
    async def csrf(request: Request, call_next: Any) -> Response:
        if request.method not in (
            "GET",
            "HEAD",
            "OPTIONS",
        ) and request.url.path.startswith("/api/"):
            cookie = request.cookies.get(auth.CSRF_COOKIE, "")
            header = request.headers.get("x-am-csrf", "")
            if not cookie or not hmac.compare_digest(cookie, header):
                return JSONResponse(
                    {"detail": "missing or wrong CSRF token"}, status_code=403
                )
        return await call_next(request)

    app.include_router(
        auth.router(
            web_url,
            api_url,
            oauth_client_id,
            oauth_secret_env,
            base_url,
            allow_dev_login,
        )
    )
    app.include_router(api.build_router())
    app.include_router(chat.build_router())

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "login": service.settings.login,
            "dev_mode": service.settings.dev_mode,
        }

    @app.post("/webhooks/github")
    async def webhook(request: Request) -> dict[str, Any]:
        if verify_webhook is None or webhook_target is None:
            raise HTTPException(404, "webhooks are not enabled")
        secret = os.environ.get(webhook_secret_env)
        body = await request.body()
        if not secret or not verify_webhook(
            secret.encode(), body, request.headers.get("x-hub-signature-256")
        ):
            raise HTTPException(401, "bad signature")
        target = webhook_target(
            request.headers.get("x-github-event", ""), json.loads(body or b"{}")
        )
        if target is None:
            return {"ignored": True}
        repo, _number = target
        record = await service.store.get_repo(repo)
        if record is None or not record.active:
            return {"ignored": True}
        events = await service.poller.poll_repo(repo)
        return {"events": len(events)}

    if STATIC.is_dir():
        if (STATIC / "assets").is_dir():
            app.mount(
                "/assets", StaticFiles(directory=STATIC / "assets"), name="assets"
            )

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            candidate = STATIC / path
            if path and candidate.is_file() and STATIC in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(STATIC / "index.html")

    return app
