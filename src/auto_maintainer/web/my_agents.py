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

"""Agent accounts on the dashboard: each person's own, and the service's sign-ins.

A signed-in person may sign each agent CLI in to their own account, or give it their
own model API, and turn that on for the work they ask for (``/api/me/agents``).
Every route here works on the signed-in person's own login and nobody else's; their
keys go in and never come back. Their own account runs only where each agent run
gets a container of its own (``Service.agents_isolated``).

Signing an agent CLI in for the service itself changes the service, so it needs
``service_gate``, as every service setting does (``/api/settings/agents``).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.agents import AgentLogin, PersonalAgent, PersonalProvider
from ..domain.settings import PROVIDER_PROTOCOLS
from ..service import Service
from .api import service_of
from .auth import User, current_user

_BASE_URL = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$")


class SetupBody(BaseModel):
    use_own: bool = False
    provider: PersonalProvider | None = None


class KeyBody(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=4000)


def _adapter(runtime: Any, name: str) -> str:
    """The adapter a runtime comes from, for its provider protocols."""
    return str(getattr(type(runtime), "default_name", name))


async def _agent_row(service: Service, user: User, name: str) -> dict[str, Any]:
    runtime = service.runtime(name)
    people = service.a.people
    setup = (await people.get(user.login)).get(name) if people else None
    login = next(
        (
            p.state
            for p in service.logins.values()
            if p.state.agent == name
            and p.state.account == user.login.lower()
            and p.state.state in ("starting", "waiting")
        ),
        None,
    )
    return {
        "name": name,
        "can_sign_in": bool(getattr(runtime, "login_args", ())),
        "signed_in": await service.signed_in(name, user.login.lower())
        if getattr(runtime, "login_args", ())
        else None,
        "protocols": sorted(PROVIDER_PROTOCOLS.get(_adapter(runtime, name), ())),
        "isolated": service.agents_isolated(name),
        "setup": (setup or PersonalAgent(agent=name)).model_dump(mode="json"),
        "login": login.model_dump(mode="json") if login else None,
    }


def _ready(service: Service, name: str) -> None:
    if service.runtime(name) is None or not service.agent_ready(name):
        raise HTTPException(404, f"no agent {name!r} is installed here")


def _login_of(service: Service, user: User, login_id: str) -> Any:
    """A sign-in the user started; nobody else's."""
    process = service.logins.get(login_id)
    if process is None or process.state.by.lower() != user.login.lower():
        raise HTTPException(404, "no such sign-in")
    return process


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/me/agents")
    async def my_agents(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The agents installed here, with how the user runs each one."""
        service = service_of(request)
        names = sorted(
            n
            for n in getattr(service.a.agents, "runtimes", None) or {}
            if service.agent_ready(n)
        )
        return {
            "personal": service.a.people is not None,
            "agents": [await _agent_row(service, user, n) for n in names],
        }

    @r.put("/me/agents/{name}")
    async def set_my_agent(
        request: Request, name: str, body: SetupBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Turn the user's own account for agent ``name`` on or off, or set their own
        model API for it (its key separately, write-only)."""
        service = service_of(request)
        _ready(service, name)
        if service.a.people is None:
            raise HTTPException(409, "this host keeps no personal agent accounts")
        if body.use_own and not service.agents_isolated(name):
            raise HTTPException(
                409,
                "agents run on the host here, where every run could read every "
                "person's sign-in, so personal accounts are off",
            )
        if body.provider is not None:
            allowed = PROVIDER_PROTOCOLS.get(_adapter(service.runtime(name), name), ())
            if body.provider.protocol not in allowed:
                raise HTTPException(
                    422, f"{name} cannot use a {body.provider.protocol} API"
                )
            if not _BASE_URL.match(body.provider.base_url.strip()):
                raise HTTPException(422, "the base URL must look like https://host/v1")
        current = (await service.a.people.get(user.login)).get(name)
        setup = PersonalAgent(
            agent=name,
            use_own=body.use_own,
            provider=body.provider,
            has_key=bool(current and current.has_key),
        )
        await service.a.people.put(user.login, setup)
        return await _agent_row(service, user, name)

    @r.put("/me/agents/{name}/key")
    async def set_my_key(
        request: Request, name: str, body: KeyBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Store the key of the user's own model API for agent ``name``."""
        service = service_of(request)
        _ready(service, name)
        if service.a.people is None:
            raise HTTPException(409, "this host keeps no personal agent accounts")
        await service.a.people.set_key(user.login, name, body.value.strip())
        return await _agent_row(service, user, name)

    @r.delete("/me/agents/{name}/key")
    async def delete_my_key(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        if service.a.people is not None:
            await service.a.people.set_key(user.login, name, None)
        return await _agent_row(service, user, name)

    @r.post("/me/agents/{name}/sign-in")
    async def sign_me_in(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Start agent ``name``'s own sign-in for the user's own account."""
        service = service_of(request)
        _ready(service, name)
        try:
            state = service.start_login(name, user.login.lower(), user.login)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return state.model_dump(mode="json")

    @r.post("/me/agents/{name}/sign-out")
    async def sign_me_out(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Remove the user's own sign-in to agent ``name`` from this host."""
        service = service_of(request)
        _ready(service, name)
        runtime = service.runtime(name)
        if service.a.people is not None:
            await service.a.people.sign_out(
                user.login, tuple(getattr(runtime, "home_paths", ()))
            )
        service.forget_signed_in(name, user.login.lower())
        return await _agent_row(service, user, name)

    @r.post("/settings/agents/{name}/sign-in")
    async def sign_service_in(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Start agent ``name``'s own sign-in for the service's account: maintainers
        of the service's repository only, as for every service setting."""
        service = service_of(request)
        decision = await service.service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        _ready(service, name)
        try:
            state = service.start_login(name, None, user.login)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return state.model_dump(mode="json")

    @r.get("/settings/agents/sign-ins")
    async def service_sign_ins(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, bool | None]:
        """Whether each installed agent CLI is signed in for the service."""
        service = service_of(request)
        names = [
            n
            for n in getattr(service.a.agents, "runtimes", None) or {}
            if service.agent_ready(n)
        ]
        return {n: await service.signed_in(n) for n in names}

    @r.get("/agent-logins/{login_id}")
    async def login_state(
        request: Request, login_id: str, user: User = Depends(current_user)
    ) -> AgentLogin:
        return _login_of(service_of(request), user, login_id).state

    @r.post("/agent-logins/{login_id}/code")
    async def login_code(
        request: Request,
        login_id: str,
        body: CodeBody,
        user: User = Depends(current_user),
    ) -> AgentLogin:
        """Hand the code the sign-in page showed to the waiting CLI."""
        process = _login_of(service_of(request), user, login_id)
        try:
            await process.submit(body.code)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return process.state

    @r.delete("/agent-logins/{login_id}")
    async def cancel_login(
        request: Request, login_id: str, user: User = Depends(current_user)
    ) -> AgentLogin:
        process = _login_of(service_of(request), user, login_id)
        await process.cancel()
        return process.state

    return r
