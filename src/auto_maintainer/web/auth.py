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

"""Dashboard sign-in (design 5.13): GitHub OAuth web and device flows, HMAC session cookies.

A GitHub token from sign-in is used once, to learn the user's login, and then
discarded. Repository permissions come from the service's own forge reader.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

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


def _login_response(request: Request, user: User, redirect: str | None = "/") -> Any:
    sessions: Sessions = request.app.state.sessions
    secure = request.app.state.cookie_secure
    response: Any = (
        RedirectResponse(redirect, status_code=303)
        if redirect
        else JSONResponse({"login": user.login})
    )
    response.set_cookie(
        SESSION_COOKIE,
        sessions.session_for(user),
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_DAYS * 86400,
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(24),
        httponly=False,
        samesite="strict",
        secure=secure,
    )
    return response


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


def router(
    web_url: str,
    api_url: str,
    client_id: str | None,
    client_secret_env: str,
    base_url: str,
    dev: bool,
) -> APIRouter:
    r = APIRouter()
    device_flows: dict[str, dict[str, Any]] = {}

    @r.get("/auth/login")
    async def login(request: Request) -> Any:
        if not client_id:
            raise HTTPException(500, "github.oauth_client_id is not configured")
        state = secrets.token_urlsafe(16)
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": f"{base_url}/auth/callback",
                "state": state,
                "scope": "read:user",
            }
        )
        response = RedirectResponse(f"{web_url}/login/oauth/authorize?{query}")
        response.set_cookie(
            "am_oauth_state", state, httponly=True, samesite="lax", max_age=600
        )
        return response

    @r.get("/auth/callback")
    async def callback(request: Request, code: str, state: str) -> Any:
        if not client_id or not hmac.compare_digest(
            state, request.cookies.get("am_oauth_state", "")
        ):
            raise HTTPException(400, "the sign-in state does not match; try again")
        secret = os.environ.get(client_secret_env)
        if not secret:
            raise HTTPException(500, f"{client_secret_env} is not set")
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            resp = await client.post(
                f"{web_url}/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": f"{base_url}/auth/callback",
                },
                headers={"Accept": "application/json"},
            )
            token = resp.json().get("access_token")
        if not token:
            raise HTTPException(401, "GitHub did not return a token")
        return _login_response(request, await _github_user(api_url, token))

    @r.post("/auth/device/start")
    async def device_start() -> dict[str, Any]:
        if not client_id:
            raise HTTPException(500, "github.oauth_client_id is not configured")
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            resp = await client.post(
                f"{web_url}/login/device/code",
                data={"client_id": client_id, "scope": "read:user"},
                headers={"Accept": "application/json"},
            )
            data = resp.json()
        flow_id = secrets.token_urlsafe(16)
        device_flows[flow_id] = {
            "device_code": data["device_code"],
            "expires": time.time() + int(data.get("expires_in", 900)),
        }
        return {
            "flow_id": flow_id,
            "user_code": data["user_code"],
            "verification_uri": data["verification_uri"],
            "interval": data.get("interval", 5),
        }

    @r.post("/auth/device/poll")
    async def device_poll(request: Request, body: dict[str, str]) -> Any:
        flow = device_flows.get(body.get("flow_id", ""))
        if flow is None or flow["expires"] < time.time():
            raise HTTPException(404, "unknown or expired sign-in")
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            resp = await client.post(
                f"{web_url}/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "device_code": flow["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            data = resp.json()
        if "access_token" not in data:
            return JSONResponse(
                {"pending": True, "error": data.get("error")}, status_code=202
            )
        device_flows.pop(body["flow_id"], None)
        return _login_response(
            request, await _github_user(api_url, data["access_token"]), redirect=None
        )

    @r.get("/auth/config")
    async def auth_config() -> dict[str, bool]:
        """Which sign-in methods this instance offers."""
        return {
            "oauth": bool(client_id),
            "device_flow": bool(client_id),
            "dev_login": dev,
        }

    @r.post("/auth/logout")
    async def logout() -> Any:
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
