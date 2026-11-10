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

"""Adding a forge from the dashboard (design 5.16), for the deployment's owner.

A new forge is a change to the host's setup, so it passes ``service_gate`` as every
service setting does. ``steps`` (``hostsetup.ForgeSteps``, from the composition root)
reads and writes the host's setup and runs the forge CLI's sign-in, with its one-time
code or (on GitLab) a personal access token, into the service's own folder for that
CLI. Only once the service has restarted in place with the new forge does it land in
``local.yaml``; a sign-in that ends without the forge kept is deleted.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api import service_of
from .auth import User, current_user

Authorize = Callable[[Request], Awaitable[dict[str, Any]]]

FLOW_SECONDS = 900


class ForgeBody(BaseModel):
    id: str = Field(min_length=1, max_length=31)
    kind: Literal["github", "gitlab"] = "gitlab"
    web_url: str = ""
    """Its web address; empty for the kind's public one (github.com, gitlab.com)."""
    api_url: str = ""
    oauth_client_id: str = ""
    """A self-managed GitLab's OAuth application, for the one-time code."""
    token: str = Field(default="", max_length=4000)
    """A personal access token instead of the one-time code (GitLab)."""


def _refused(e: Exception, default: int) -> HTTPException:
    return HTTPException(getattr(e, "status", default), str(e))


def build_router(
    steps: Any,
    maintainer: Authorize,
    on_host_change: Callable[[Any], Awaitable[None]],
) -> APIRouter:
    r = APIRouter()
    flows: dict[str, dict[str, Any]] = {}

    def view(request: Request) -> list[dict[str, Any]]:
        settings = service_of(request).settings
        names = settings.identities | {settings.login}

        def account(forge_id: str, default: bool) -> str | None:
            """The login the service acts as there, as that forge spells it."""
            for name in sorted(names):
                head, sep, login = name.partition(":")
                if default and not sep:
                    return name
                if sep and head == forge_id:
                    return login
            return None

        return [
            {**f, "default": i == 0, "signed_in_as": account(f["id"], i == 0)}
            for i, f in enumerate(steps.forges())
        ]

    async def finish(forge: Any, login: Any) -> list[dict[str, Any]]:
        """Check the new forge's account, restart with it, and only then keep it.
        A sign-in that ends here without the forge kept is deleted."""
        try:
            token = await login.token()
            if not token:
                raise HTTPException(401, "the forge CLI holds no sign-in; try again")
            try:
                who = await steps.account(forge, token)
                cfg = steps.with_forge(forge)
            except ValueError as e:
                raise _refused(e, 401) from e
            try:
                await on_host_change(cfg)
            except Exception as e:
                raise HTTPException(
                    502, f"the service could not start with {forge.id}: {e}"
                ) from e
        except BaseException:
            login.discard()
            raise
        steps.keep(cfg)
        return [{"added": forge.id, "signed_in_as": who}]

    @r.get("/api/settings/forges")
    async def list_forges(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The forges this service serves, the default first, and the account it
        acts as on each."""
        return {"forges": view(request)}

    @r.post("/api/settings/forges")
    async def add_forge(request: Request, body: ForgeBody) -> Any:
        """Begin adding a forge: the service signs in there, with a personal access
        token at once, or with the forge CLI's one-time code (then ``.../poll``)."""
        await maintainer(request)
        token = body.token.strip()
        try:
            forge = steps.new(
                body.id, body.kind, body.web_url, body.api_url, body.oauth_client_id
            )
            if token and forge.kind != "gitlab":
                raise HTTPException(
                    422, "a GitHub forge signs in with the one-time code"
                )
            await steps.install_cli(forge)
        except ValueError as e:
            raise _refused(e, 503) from e
        login = steps.login(forge)
        try:
            if token:
                await login.with_token(token)
            else:
                started = await login.start()
        except ValueError as e:
            login.discard()
            raise HTTPException(503, str(e)) from e
        if token:
            return {"done": True, "forges": await finish(forge, login)}
        now = time.time()
        for fid, flow in list(flows.items()):
            if flow["exp"] < now:
                flow["login"].discard()
                flows.pop(fid, None)
        flow_id = secrets.token_urlsafe(12)
        flows[flow_id] = {"login": login, "forge": forge, "exp": now + FLOW_SECONDS}
        return {"flow_id": flow_id, **started}

    @r.post("/api/settings/forges/poll")
    async def poll_forge(request: Request, body: dict[str, str]) -> Any:
        await maintainer(request)
        flow = flows.get(body.get("flow_id", ""))
        if flow is None:
            raise HTTPException(404, "unknown or expired sign-in")
        state = flow["login"].state()
        if state == "pending":
            return JSONResponse({"pending": True}, status_code=202)
        flows.pop(body.get("flow_id", ""), None)
        if state != "done":
            flow["login"].discard()
            raise HTTPException(
                401, "the forge did not complete the sign-in; try again"
            )
        return {"done": True, "forges": await finish(flow["forge"], flow["login"])}

    return r
