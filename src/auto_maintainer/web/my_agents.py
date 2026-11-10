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

A signed-in person may sign each agent CLI in to their own account, or keep model
APIs of their own (``/api/me/providers``) and run an agent on one of them, with the
model and reasoning level they choose, for the work they ask for
(``/api/me/agents``). Their APIs serve only their own agents and tasks: nobody
else's run ever gets them. Every route here works on the signed-in person's own
login and nobody else's; their keys go in and never come back. Their own account
runs only where each agent run gets a container of its own
(``Service.agents_isolated``). Asking an endpoint about its models goes out from
the host, so a person's requests may not reach the host itself or a link-local
address (``model_apis.refused_address``).

Signing an agent CLI in for the service itself changes the service, so it needs
``service_gate``, as every service setting does (``/api/settings/agents``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.agents import AgentLogin, PersonalAgent
from ..domain.ports import PersonalAgents
from ..domain.settings import PROVIDER_PROTOCOLS, REASONING_LEVELS, protocol_for
from ..service import Service
from .api import service_of, visible_repos
from .auth import User, current_user
from .model_apis import ApiForm, ProbeBody, list_models, refused_address
from .settings import examined, provider_row


class SetupBody(BaseModel):
    use_own: bool = False
    provider: str | None = Field(default=None, max_length=64)
    """One of the user's own model APIs; none, the CLI's own sign-in."""
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = None


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=4000)


class SignInBody(BaseModel):
    option: str | None = Field(default=None, max_length=64)
    """What to sign in to, where the CLI offers several (``sign_in_options``)."""


def sign_in_options(runtime: Any) -> list[dict[str, str]]:
    """The account kinds a CLI's sign-in offers, for the dashboard to pick from."""
    return [
        {"id": o, "label": label} for o, label in getattr(runtime, "login_options", ())
    ]


def can_sign_in(runtime: Any) -> bool:
    can = getattr(runtime, "can_sign_in", None)
    return bool(can and can())


def _adapter(runtime: Any, name: str) -> str:
    """The adapter a runtime comes from, for its provider protocols."""
    return str(getattr(type(runtime), "default_name", name))


def _people(service: Service) -> PersonalAgents:
    if service.a.people is None:
        raise HTTPException(409, "this host keeps no personal agent accounts")
    return service.a.people


async def _providers(service: Service, user: User) -> list[dict[str, Any]]:
    """The user's own model APIs, as the forms show them."""
    people = service.a.people
    if people is None:
        return []
    keyed = await people.keyed(user.login)
    setups = await people.get(user.login)
    return [
        {
            **provider_row(pid, p),
            "has_key": pid in keyed,
            "used_by": sorted(n for n, s in setups.items() if s.provider == pid),
        }
        for pid, p in sorted((await people.providers(user.login)).items())
    ]


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
        "can_sign_in": can_sign_in(runtime),
        "sign_in_options": sign_in_options(runtime),
        "signed_in": await service.signed_in(name, user.login.lower())
        if can_sign_in(runtime)
        else None,
        "adapter": _adapter(runtime, name),
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
            "providers": await _providers(service, user),
        }

    @r.put("/me/agents/{name}")
    async def set_my_agent(
        request: Request, name: str, body: SetupBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Turn the user's own account for agent ``name`` on or off, and choose
        whether it runs on their CLI sign-in or on one of their own model APIs, with
        which model and reasoning level."""
        service = service_of(request)
        _ready(service, name)
        people = _people(service)
        if body.use_own and not service.agents_isolated(name):
            raise HTTPException(
                409,
                "agents run on the host here, where every run could read every "
                "person's sign-in, so personal accounts are off",
            )
        if body.effort and body.effort not in REASONING_LEVELS:
            raise HTTPException(
                422, f"the reasoning level is one of {', '.join(REASONING_LEVELS)}"
            )
        model = body.model or None
        if body.provider:
            api = (await people.providers(user.login)).get(body.provider)
            if api is None:
                raise HTTPException(422, f"you have no model API {body.provider!r}")
            model = model or (api.model_ids[0] if api.models else None)
            if api.models and model not in api.model_ids:
                raise HTTPException(422, f"{body.provider} lists no model {model!r}")
            spec = api.model(model)
            adapter = _adapter(service.runtime(name), name)
            if protocol_for(adapter, api.protocol, spec) is None:
                raise HTTPException(
                    422, f"{name} cannot use {model or body.provider} over its APIs"
                )
            if (
                body.effort
                and spec
                and spec.reasoning
                and body.effort not in spec.reasoning
            ):
                raise HTTPException(
                    422,
                    f"{model} takes the reasoning levels {', '.join(spec.reasoning)}",
                )
        setup = PersonalAgent(
            agent=name,
            use_own=body.use_own,
            provider=body.provider or None,
            model=model if body.provider else None,
            effort=body.effort or None,
        )
        await people.put(user.login, setup)
        return await _agent_row(service, user, name)

    @r.put("/me/providers/{provider_id}")
    async def put_my_provider(
        request: Request,
        provider_id: str,
        body: ApiForm,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Add or change one of the user's own model APIs. A key typed into the form
        is stored apart, for the runs of the user's own tasks only, and never sent
        back."""
        service = service_of(request)
        people = _people(service)
        try:
            provider = body.model_copy(update={"api_key": None}).provider()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        try:
            await people.put_provider(user.login, provider_id, provider)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if body.key:
            await people.set_provider_key(user.login, provider_id, body.key.strip())
        return {"providers": await _providers(service, user)}

    @r.delete("/me/providers/{provider_id}")
    async def delete_my_provider(
        request: Request, provider_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Remove one of the user's model APIs with its key; their agents that ran
        on it go back to their CLI's sign-in."""
        service = service_of(request)
        people = _people(service)
        if provider_id not in await people.providers(user.login):
            raise HTTPException(404, f"you have no model API {provider_id!r}")
        await people.put_provider(user.login, provider_id, None)
        return {"providers": await _providers(service, user)}

    async def _probe_key(service: Service, user: User, body: ProbeBody) -> str | None:
        """The key a personal probe uses, after checking the user may make one: a
        person with a repository here, to an address the host may reach for them."""
        if not await visible_repos(service, user):
            raise HTTPException(403, "only people with a repository here ask endpoints")
        refused = await refused_address(body.base_url)
        if refused:
            raise HTTPException(422, refused)
        if body.key:
            return body.key.strip()
        if body.provider:
            return await _people(service).provider_key(user.login, body.provider)
        return None

    @r.post("/me/providers/query")
    async def query_my_provider(
        request: Request, body: ProbeBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The models one of the user's endpoints lists, with what it says of each,
        before or after it is saved; spends no tokens."""
        service = service_of(request)
        key = await _probe_key(service, user, body)
        return await list_models(body.protocol, body.base_url, key)

    @r.post("/me/providers/examine")
    async def examine_my_provider(
        request: Request, body: ProbeBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Talk to one of the user's models to learn what it can do, on their key;
        spends a few thousand of their tokens."""
        service = service_of(request)
        if not body.model:
            raise HTTPException(422, "name the model to examine")
        key = await _probe_key(service, user, body)
        return await examined(body, key)

    @r.post("/me/agents/{name}/sign-in")
    async def sign_me_in(
        request: Request,
        name: str,
        body: SignInBody | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Start agent ``name``'s own sign-in for the user's own account, to the
        account kind ``option`` where the CLI offers several."""
        service = service_of(request)
        _ready(service, name)
        try:
            state = service.start_login(
                name, user.login.lower(), user.login, body.option if body else None
            )
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
        request: Request,
        name: str,
        body: SignInBody | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Start agent ``name``'s own sign-in for the service's account: its owner
        only, as for every service setting."""
        service = service_of(request)
        decision = await service.service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        _ready(service, name)
        try:
            state = service.start_login(
                name, None, user.login, body.option if body else None
            )
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
