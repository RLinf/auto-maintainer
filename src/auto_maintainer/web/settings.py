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

"""The service's settings on the dashboard (design 8.1 and 5.13).

Everyone signed in can read them. Changing them, setting an API key, or testing a
provider needs the service gate: a live check of the user's permission on the
service's repository (``ConfigGate.check_service``). A change from anyone else waits
as a proposal for a maintainer. API key values only ever travel inward: no response
carries one.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.memory import MemoryChangeState
from ..domain.records import AuditEntry
from ..domain.settings import (
    PROVIDER_PROTOCOLS,
    SECRET_NAME,
    LabMachine,
    Provider,
    SettingsError,
    can_bypass,
    effective_agents,
    effective_routing,
    machine_problems,
    secret_names,
    settings_diff,
)
from ..service import Service
from .api import service_of, visible_repos
from .auth import User, current_user


class DocumentBody(BaseModel):
    yaml: str
    title: str = Field(default="settings: update", max_length=120)


class SecretBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


async def _require_maintainer(service: Service, user: User) -> None:
    decision = await service.service_gate(user.login)
    if not decision.allowed:
        raise HTTPException(403, decision.reason)


async def settings_view(service: Service, user: User) -> dict[str, Any]:
    """Everything the Settings page shows, with each value's source."""
    text, legacy = await service.settings_text()
    config = service.service_config
    agents = effective_agents(config, legacy)
    host_agent = service.host_values["default_agent"]
    routes = effective_routing(service.default_routing, config, host_agent)
    names = secret_names(config)
    secrets = (
        {s.name: s for s in await service.a.secrets.describe(names)}
        if service.a.secrets is not None
        else {}
    )
    system = config.system
    gate = await service.service_gate(user.login)
    pending = [
        c
        for c in await service.store.list_memory_changes(MemoryChangeState.OPEN)
        if c.service
    ]
    return {
        "access": gate.model_dump(mode="json"),
        "repo": service.settings_repo(),
        "document": text,
        "status": service.settings_status.model_dump(mode="json"),
        "agents": [
            {
                "name": name,
                **cfg.model_dump(
                    mode="json",
                    include={
                        "adapter",
                        "enabled",
                        "model",
                        "effort",
                        "provider",
                        "budget",
                        "allow_bypass",
                        "permissions",
                        "binary",
                    },
                ),
                "can_bypass": can_bypass(cfg),
                "installed": service.agent_ready(name) if cfg.enabled else None,
                "confined": bool(cfg.command_prefix),
                "from_settings": name in config.agents or name in legacy,
                "providers_supported": sorted(PROVIDER_PROTOCOLS.get(cfg.adapter, ())),
            }
            for name, cfg in agents.items()
        ],
        "providers": [
            {
                "id": pid,
                **p.model_dump(mode="json"),
                "key": secrets[p.api_key].model_dump(mode="json")
                if p.api_key in secrets
                else None,
                "used_by": sorted(n for n, a in agents.items() if a.provider == pid),
            }
            for pid, p in config.providers.items()
        ],
        "default_agent": {
            "name": config.default_agent or host_agent,
            "source": "settings"
            if config.default_agent
            else "setup"
            if host_agent
            else None,
        },
        "routing": [
            {
                "task": task,
                **route.model_dump(
                    mode="json",
                    include={
                        "primary",
                        "fallback",
                        "verifier",
                        "model",
                        "effort",
                        "timeout_seconds",
                        "permissions",
                    },
                ),
                "from_settings": task in config.routing,
                "runs_on": service.runs_on(route),
            }
            for task, route in routes.items()
        ],
        "system": {
            "poll_seconds": service.settings.poll_seconds,
            "max_public_writes_per_hour": service.settings.write_caps[0],
            "max_repo_public_writes_per_hour": service.settings.write_caps[1],
            "from_settings": sorted(system.model_dump(exclude_none=True)),
        },
        "host": {
            "dev_mode": service.settings.dev_mode,
            "paused": await service.pipeline.paused(None),
            "code_repo": service.settings.code_repo,
            "memory_repo": service.a.memory.remote_repo(),
            "base_url": service.settings.base_url,
            "sandbox_repo": service.settings.sandbox_repo,
            "operators": sorted(service.settings.operators),
        },
        "secrets": [s.model_dump(mode="json") for s in secrets.values()],
        "pending": [c.model_dump(mode="json") for c in pending],
        "labs": [
            {"name": name, **machine.model_dump(mode="json")}
            for name, machine in config.labs.items()
        ],
    }


class AgentModeBody(BaseModel):
    permissions: Literal["auto", "restricted", "bypass"] | None = None
    """The mode the agent runs every task in; ``None`` follows each task's route."""


def _with_agent_mode(text: str, name: str, mode: str | None) -> str:
    """The settings document with agent ``name``'s own permission mode set (or
    removed when ``None``); its other settings are kept."""
    return _with_agent_fields(text, name, {"permissions": mode})


def _settings_data(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text or "") or {}
    if not isinstance(data, dict):
        raise SettingsError(["the settings file is not a mapping"])
    data.setdefault("schema", 1)
    return data


def _with_default_agent(text: str, name: str | None) -> str:
    """The settings document with ``default_agent`` set, or removed when ``None``."""
    data = _settings_data(text)
    if name:
        data["default_agent"] = name
    else:
        data.pop("default_agent", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _with_agent_fields(text: str, name: str, fields: dict[str, Any]) -> str:
    """The settings document with agent ``name``'s ``fields`` set (each ``None``
    removed, so the default applies); its other settings are kept."""
    data = _settings_data(text)
    agents = dict(data.get("agents") or {})
    entry = dict(agents.get(name) or {})
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if entry:
        agents[name] = entry
    else:
        agents.pop(name, None)
    if agents:
        data["agents"] = agents
    else:
        data.pop("agents", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class AgentModelBody(BaseModel):
    model: str | None = None
    effort: str | None = None


class DefaultAgentBody(BaseModel):
    agent: str | None = None


class MachineBody(BaseModel):
    machine: LabMachine
    title: str = ""


def _with_machine(text: str, name: str, machine: LabMachine | None) -> str:
    """The settings document with machine ``name`` set (or removed when ``None``)."""
    data = yaml.safe_load(text or "") or {}
    if not isinstance(data, dict):
        raise SettingsError(["the settings file is not a mapping"])
    data.setdefault("schema", 1)
    labs = dict(data.get("labs") or {})
    if machine is None:
        labs.pop(name, None)
    else:
        labs[name] = machine.model_dump(mode="json", exclude_defaults=True)
    if labs:
        data["labs"] = labs
    else:
        data.pop("labs", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _describe_probe_error(status: int | None, error: Exception | None) -> str:
    if error is not None:
        return "could not reach the endpoint; check the base URL and the host's network"
    if status in (401, 403):
        return "the endpoint rejected the key"
    if status == 404:
        return (
            "there is no model list at this URL; check the base URL (many end in /v1)"
        )
    if status == 429:
        return "the endpoint is rate limiting this key"
    if status is not None and status >= 500:
        return f"the endpoint failed ({status})"
    return f"unexpected answer ({status})"


async def probe_models(provider: Provider, key: str | None) -> dict[str, Any]:
    """List the provider's models with its key; spends no tokens. Returns model ids only."""
    base = provider.base_url
    headers = {"Accept": "application/json"}
    if provider.protocol == "anthropic":
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
            headers["Authorization"] = f"Bearer {key}"
    else:
        url = f"{base}/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "error": _describe_probe_error(None, e), "models": []}
    if res.status_code != 200:
        return {
            "ok": False,
            "error": _describe_probe_error(res.status_code, None),
            "status": res.status_code,
            "models": [],
        }
    try:
        data = res.json().get("data", [])
        models = sorted(
            {str(m["id"]) for m in data if isinstance(m, dict) and "id" in m}
        )[:200]
    except (ValueError, AttributeError):
        return {"ok": False, "error": "the answer was not a model list", "models": []}
    return {"ok": True, "models": models, "status": 200}


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/settings")

    @r.get("")
    async def get_settings(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        return await settings_view(service_of(request), user)

    @r.post("/check")
    async def check(
        request: Request, body: DocumentBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Check a draft without saving it: problems, and the diff against what runs."""
        service = service_of(request)
        current, _ = await service.settings_text()
        try:
            await service.check_settings_text(body.yaml)
        except SettingsError as e:
            return {
                "ok": False,
                "problems": e.problems,
                "diff": settings_diff(current, body.yaml),
            }
        return {"ok": True, "problems": [], "diff": settings_diff(current, body.yaml)}

    @r.put("")
    async def save(
        request: Request, body: DocumentBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        current, _ = await service.settings_text()
        try:
            change, decision = await service.propose_settings(
                body.yaml, user.login, body.title
            )
        except SettingsError as e:
            raise HTTPException(
                422, {"message": "the settings do not check", "problems": e.problems}
            ) from e
        stored = await service.store.get_memory_change(change.id)
        return {
            "change": stored.model_dump(mode="json") if stored else None,
            "approved": decision.allowed,
            "gate": decision.model_dump(mode="json"),
            "diff": settings_diff(current, body.yaml),
            "status": service.settings_status.model_dump(mode="json"),
        }

    @r.put("/secrets/{name}")
    async def set_secret(
        request: Request,
        name: str,
        body: SecretBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        await _require_maintainer(service, user)
        if service.a.secrets is None:
            raise HTTPException(409, "this service has no secret store")
        if not SECRET_NAME.match(name):
            raise HTTPException(
                422, "secret names are capitals, such as DEEPSEEK_API_KEY"
            )
        await service.a.secrets.set(name, body.value, user.login)
        await service.store.add_audit(
            AuditEntry(kind="config", actor=user.login, summary=f"set secret {name}")
        )
        await service.reload_settings()
        [info] = await service.a.secrets.describe([name])
        return info.model_dump(mode="json")

    @r.delete("/secrets/{name}")
    async def delete_secret(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        await _require_maintainer(service, user)
        if service.a.secrets is None:
            raise HTTPException(409, "this service has no secret store")
        await service.a.secrets.delete(name)
        await service.store.add_audit(
            AuditEntry(
                kind="config", actor=user.login, summary=f"removed secret {name}"
            )
        )
        await service.reload_settings()
        return {"name": name, "deleted": True}

    @r.put("/agents/{name}/permissions")
    async def set_agent_mode(
        request: Request,
        name: str,
        body: AgentModeBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Give an agent its own permission mode for every task (``bypass`` skips
        its CLI's permission checks on the team's PRs), or let it follow each route:
        a settings change like any other."""
        service = service_of(request)
        text, legacy = await service.settings_text()
        if name not in effective_agents(service.service_config, legacy):
            raise HTTPException(404, f"there is no agent named {name!r}")
        mode = body.permissions or "the task's route"
        return await save(
            request,
            DocumentBody(
                yaml=_with_agent_mode(text, name, body.permissions),
                title=f"settings: {name} runs in {mode} mode",
            ),
            user,
        )

    @r.put("/agents/{name}/model")
    async def set_agent_model(
        request: Request,
        name: str,
        body: AgentModelBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Choose the model and reasoning level an agent uses when a route names
        none; empty keeps the CLI's own. A settings change like any other."""
        service = service_of(request)
        text, legacy = await service.settings_text()
        if name not in effective_agents(service.service_config, legacy):
            raise HTTPException(404, f"there is no agent named {name!r}")
        what = ", ".join(
            f"{k} {v}" for k, v in (("model", body.model), ("effort", body.effort)) if v
        )
        return await save(
            request,
            DocumentBody(
                yaml=_with_agent_fields(
                    text,
                    name,
                    {"model": body.model or None, "effort": body.effort or None},
                ),
                title=f"settings: {name} uses {what or 'its own defaults'}",
            ),
            user,
        )

    @r.put("/default-agent")
    async def set_default_agent(
        request: Request, body: DefaultAgentBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Choose the agent every task tries first; empty keeps setup's choice."""
        service = service_of(request)
        text, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_default_agent(text, body.agent or None),
                title=f"settings: {body.agent} runs every task first"
                if body.agent
                else "settings: the agent chosen during setup runs every task first",
            ),
            user,
        )

    @r.put("/labs/{name}")
    async def set_machine(
        request: Request,
        name: str,
        body: MachineBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Add or change a test machine: a settings change like any other, applied at
        once for a maintainer of the service's repository, a proposal otherwise."""
        service = service_of(request)
        problems = machine_problems(name, body.machine)
        if problems:
            raise HTTPException(
                422, {"message": "the machine does not check", "problems": problems}
            )
        current, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_machine(current, name, body.machine),
                title=body.title or f"settings: test machine {name}",
            ),
            user,
        )

    @r.delete("/labs/{name}")
    async def remove_machine(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        current, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_machine(current, name, None),
                title=f"settings: remove test machine {name}",
            ),
            user,
        )

    @r.post("/labs/{name}/check")
    async def check_machine(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Connect to the machine and report what it is and its GPUs."""
        service = service_of(request)
        machine = service.machines.get(name)
        if not (await service.service_gate(user.login)).allowed:
            visible = await visible_repos(service, user) if machine else []
            if not (machine and any(machine.serves(repo) for repo in visible)):
                await _require_maintainer(service, user)
        runner = service.a.lab_runner
        if runner is None or machine is None:
            raise HTTPException(404, f"no test machine named {name!r} is in force")
        try:
            return await runner.check(name)
        except Exception as e:  # report the failure to the page, not as a 500
            return {"ok": False, "error": str(e)[-500:]}

    @r.post("/providers/{provider_id}/test")
    async def test_provider(
        request: Request, provider_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        await _require_maintainer(service, user)
        provider = service.service_config.providers.get(provider_id)
        if provider is None:
            raise HTTPException(404, f"no provider named {provider_id!r} is in force")
        key = (
            await service.a.secrets.get(provider.api_key)
            if provider.api_key and service.a.secrets is not None
            else None
        )
        if provider.api_key and not key:
            return {
                "ok": False,
                "error": f"the key {provider.api_key} is not set yet",
                "models": [],
            }
        return await probe_models(provider, key)

    return r
