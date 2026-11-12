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

import json
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.ids import new_id
from ..domain.labs import owns
from ..domain.memory import MemoryChangeState
from ..domain.records import AuditEntry
from ..domain.settings import (
    PROVIDER_ID,
    PROVIDER_PROTOCOLS,
    SECRET_NAME,
    LabMachine,
    ModelSpec,
    Provider,
    SettingsError,
    TokenLimits,
    agents_for,
    can_bypass,
    effective_agents,
    effective_routing,
    machine_problems,
    parse_ssh_config,
    secret_names,
    settings_diff,
)
from ..playbooks.prompts import schema_hint
from ..service import Service
from .api import service_of, visible_repos
from .auth import User, current_user
from .model_apis import ApiForm, ProbeBody, examine, list_models


class DocumentBody(BaseModel):
    yaml: str
    title: str = Field(default="settings: update", max_length=120)


class SecretBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class SandboxBody(BaseModel):
    enabled: bool = True
    network: Literal["bridge", "host"] = "bridge"
    cpus: float = Field(default=2.0, gt=0, le=128)
    memory: str = Field(default="8g", pattern=r"^\d+(\.\d+)?[kmgKMG]?$")


async def _require_maintainer(service: Service, user: User) -> None:
    decision = await service.service_gate(user.login)
    if not decision.allowed:
        raise HTTPException(403, decision.reason)


def can_sign_in(runtime: Any) -> bool:
    """Whether an agent CLI has a sign-in the dashboard can run (dsh has none: it
    runs only on a model API)."""
    can = getattr(runtime, "can_sign_in", None)
    return bool(can and can())


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
    own = set(await service.own_agents(user.login))
    setups = await service.a.people.get(user.login) if own and service.a.people else {}
    return {
        "access": gate.model_dump(mode="json"),
        "repo": service.settings_repo(),
        "owner": service.settings.owner,
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
                        "quick_model",
                        "effort",
                        "provider",
                        "budget",
                        "allow_bypass",
                        "permissions",
                        "binary",
                        "approve_commands",
                    },
                ),
                "can_bypass": can_bypass(cfg),
                "installed": service.agent_ready(name) if cfg.enabled else None,
                "usable": service.agent_usable(name) if cfg.enabled else False,
                # Whether the work this person asks for runs on their own account of
                # it, and which: what pickers offer them and try first.
                "mine": (
                    "api" if setups.get(name) and setups[name].provider else "sign-in"
                )
                if name in own
                else None,
                "can_sign_in": can_sign_in(service.runtime(name)),
                "confined": bool(cfg.command_prefix),
                "from_settings": name in config.agents or name in legacy,
                "providers_supported": sorted(PROVIDER_PROTOCOLS.get(cfg.adapter, ())),
                "sign_in_options": [
                    {"id": o, "label": label}
                    for o, label in getattr(service.runtime(name), "login_options", ())
                ],
            }
            for name, cfg in agents.items()
        ],
        "providers": [
            {
                **provider_row(pid, p),
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
            "agent_slots": service.engine.slots("agent")["size"],
            "agent_slots_in_use": {
                k: service.engine.slots("agent")[k] for k in ("people", "service")
            },
            "from_settings": sorted(system.model_dump(exclude_none=True)),
        },
        "host": {
            "dev_mode": service.settings.dev_mode,
            "standby": service.settings.standby,
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
        "installable": service.installable(),
        "installs": {
            name: state.model_dump(mode="json")
            for name, state in service.installs.items()
        },
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
    quick_model: str | None = None
    """The model for quick tasks; left out, the agent keeps the one it has."""
    provider: str | None = None
    """The model provider the agent runs on; sent as ``null``, its CLI's own
    sign-in. Left out, the agent keeps the one it has."""


class DefaultAgentBody(BaseModel):
    agent: str | None = None


class TokenLimitsBody(BaseModel):
    limits: TokenLimits


def _with_token_limits(text: str, limits: TokenLimits) -> str:
    """The settings document with ``token_limits`` set, or removed when it limits
    nobody."""
    data = _settings_data(text)
    value = limits.model_dump(mode="json", exclude_none=True)
    if not value.get("default") and not value.get("people"):
        data.pop("token_limits", None)
    else:
        # A person's ``{}`` stays: it means no limit for them.
        data["token_limits"] = {k: v for k, v in value.items() if v}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class AgentSlotsBody(BaseModel):
    slots: int | None = Field(default=None, ge=1, le=16)
    """``None`` keeps the host's value (``pools.agent`` in ``local.yaml``)."""


def _with_agent_slots(text: str, slots: int | None) -> str:
    """The settings document with ``system.agent_slots`` set, or removed when
    ``None``; the other system values are kept."""
    data = _settings_data(text)
    system = dict(data.get("system") or {})
    if slots is None:
        system.pop("agent_slots", None)
    else:
        system["agent_slots"] = slots
    if system:
        data["system"] = system
    else:
        data.pop("system", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class MachineBody(BaseModel):
    machine: LabMachine
    ssh_config: str | None = Field(default=None, max_length=4000)
    """The connection as an ``~/.ssh/config`` entry; its fields replace the machine's
    (``parse_ssh_config``)."""
    title: str = ""


def _connected(body: MachineBody) -> LabMachine:
    """``body.machine`` with the connection of ``body.ssh_config``, if any."""
    machine = body.machine
    if body.ssh_config is None or machine.transport != "ssh":
        return machine
    fields, problems = parse_ssh_config(body.ssh_config)
    if problems:
        raise HTTPException(
            422, {"message": "the SSH entry does not check", "problems": problems}
        )
    return machine.model_copy(
        update={
            "host": fields.get("host"),
            "user": fields.get("user"),
            "port": fields.get("port"),
            "proxy_jump": fields.get("proxy_jump"),
            "identity_file": fields.get("identity_file"),
        }
    )


class MachineRules(BaseModel):
    """What the service enforces on a test machine, drafted from its description."""

    workdir: str = Field(
        description="The folder runs go in on the machine: the one the description names, else the current one."
    )
    setup: str = Field(
        default="",
        description="Shell lines run before every reproduction to prepare the environment the description names; empty when it names none.",
    )
    timeout_minutes: int = Field(
        ge=1, le=360, description="The longest a run may take, in minutes."
    )
    forbid: list[str] = Field(
        default_factory=list,
        description="Python regular expressions for the commands and paths the description forbids.",
    )
    max_gpu_memory_mib: int = Field(
        ge=0, description="A GPU with less memory in use than this counts as free."
    )
    max_gpu_util_pct: int = Field(
        ge=0, le=100, description="A GPU at or below this utilization counts as free."
    )
    left_to_agent: list[str] = Field(
        default_factory=list,
        description="The description's rules none of the fields above can enforce; the reproduction agent follows them from the description.",
    )


DRAFT = """You turn the description a maintainer wrote of test machine {name} into the settings the service enforces when it runs a PR's code there (a reproduction). For each run the service makes a fresh folder under `workdir`, runs the `setup` lines, then the reproduction's own setup and script; it refuses a script that matches a `forbid` pattern; it stops the run after the time limit; and it uses a GPU only when the memory in use is below `max_gpu_memory_mib` and the utilization at most `max_gpu_util_pct`.
- workdir: the folder the description names for runs, else the current one; a folder of its own, never /, a home directory, or /tmp.
- setup: the commands that prepare the environment the description names, such as activating a conda or virtual environment, loading modules, or exporting variables like CUDA_VISIBLE_DEVICES for the GPUs runs may use. Only what the description asks for; no installs it does not mention.
- timeout_minutes: the limit the description states, else the current one.
- forbid: Python regular expressions for the commands and paths the description forbids, such as \\bray\\s+stop\\b or /data/datasets, each tight enough not to refuse unrelated commands.
- GPU thresholds: what the description says makes a GPU free, else the current values.
- left_to_agent: the description's rules that none of these fields can enforce; the reproduction agent reads the description and follows them.
Do not run commands or change files: answer from the description and the current settings."""


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


def provider_document(provider: Provider) -> dict[str, Any]:
    """``provider`` as the settings file writes it: each model by its id alone when
    nothing else is known of it."""
    out = provider.model_dump(mode="json", exclude_none=True, exclude={"models"})
    out["models"] = [
        m.id
        if not (fields := m.model_dump(mode="json", exclude_defaults=True)).keys()
        - {"id"}
        else fields
        for m in provider.models
    ]
    return out


def _with_provider(text: str, provider_id: str, provider: Provider | None) -> str:
    """The settings document with provider ``provider_id`` set, or removed when
    ``None``; everything else is kept."""
    data = _settings_data(text)
    providers = dict(data.get("providers") or {})
    if provider is None:
        providers.pop(provider_id, None)
    else:
        providers[provider_id] = provider_document(provider)
    if providers:
        data["providers"] = providers
    else:
        data.pop("providers", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def provider_row(provider_id: str, provider: Provider) -> dict[str, Any]:
    """A provider as the forms show it, with the agents each model works with."""
    return {
        "id": provider_id,
        **provider.model_dump(mode="json"),
        "works_with": {m.id: agents_for(m, provider.protocol) for m in provider.models},
    }


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

    @r.post("/agents/{name}/install")
    async def install_agent(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Install agent ``name``'s CLI on this host, from npm when its publisher
        ships it there, else from the publisher's release; maintainers of the
        service's repository only, as for every service setting."""
        service = service_of(request)
        await _require_maintainer(service, user)
        try:
            state = service.start_install(name, user.login)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return state.model_dump(mode="json")

    @r.get("/agents/latest")
    async def latest_versions(
        request: Request, refresh: bool = False, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The latest version of each agent CLI this host can install, read from its
        publisher at most every few hours; ``refresh`` reads it now, for the owner of
        the service, who upgrades them."""
        service = service_of(request)
        if refresh:
            await _require_maintainer(service, user)
        return await service.latest_versions(refresh)

    @r.get("/sandbox")
    async def sandbox_view(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Where agents run on this host: each task in a container of its own, or on
        the host; its state, network, and limits."""
        service = service_of(request)
        control = service.a.agent_sandbox
        if control is None:
            return {"available": False}
        decision = await service.service_gate(user.login)
        return {
            "available": True,
            **control.describe(),
            "can_configure": decision.allowed,
            "reason": decision.reason,
        }

    @r.put("/sandbox")
    async def set_sandbox(
        request: Request, body: SandboxBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Change it for this host (``local.yaml``); the owner of the service's
        repository only. The network and limits apply to the next agent run."""
        service = service_of(request)
        await _require_maintainer(service, user)
        control = service.a.agent_sandbox
        if control is None:
            raise HTTPException(409, "this service has no agent sandbox")
        try:
            await control.configure(
                enabled=body.enabled,
                network=body.network,
                cpus=body.cpus,
                memory=body.memory,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        await service.store.add_audit(
            AuditEntry(
                kind="config",
                actor=user.login,
                summary=f"agent sandbox: {'on' if body.enabled else 'off'}, "
                f"{body.network} network, {body.cpus:g} CPUs, {body.memory} memory",
            )
        )
        return {"available": True, **control.describe(), "can_configure": True}

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
        fields: dict[str, Any] = {
            "model": body.model or None,
            "effort": body.effort or None,
        }
        if "provider" in body.model_fields_set:
            fields["provider"] = body.provider or None
            if "quick_model" not in body.model_fields_set:
                fields["quick_model"] = None  # another provider's model
        if "quick_model" in body.model_fields_set:
            fields["quick_model"] = body.quick_model or None
        what = ", ".join(
            f"{k} {v}"
            for k, v in (
                ("provider", fields.get("provider", "")),
                ("model", body.model),
                ("effort", body.effort),
                ("quick tasks", body.quick_model),
            )
            if v
        )
        return await save(
            request,
            DocumentBody(
                yaml=_with_agent_fields(text, name, fields),
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

    @r.put("/token-limits")
    async def set_token_limits(
        request: Request, body: TokenLimitsBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """How many tokens each person's requests may use on the service's agents:
        a default for everyone and overrides by login; applies to the next run."""
        service = service_of(request)
        text, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_token_limits(text, body.limits),
                title="settings: token limits per person",
            ),
            user,
        )

    @r.put("/agent-slots")
    async def set_agent_slots(
        request: Request, body: AgentSlotsBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """How many agent jobs run at once for people's requests; applies at once."""
        service = service_of(request)
        text, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_agent_slots(text, body.slots),
                title=f"settings: {body.slots} agent jobs at once"
                if body.slots
                else "settings: the host's number of agent jobs at once",
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
        once for the owner of the service's repository, a proposal otherwise."""
        service = service_of(request)
        machine = _connected(body)
        problems = machine_problems(name, machine)
        if problems:
            raise HTTPException(
                422, {"message": "the machine does not check", "problems": problems}
            )
        current, _ = await service.settings_text()
        return await save(
            request,
            DocumentBody(
                yaml=_with_machine(current, name, machine),
                title=body.title or f"settings: test machine {name}",
            ),
            user,
        )

    @r.post("/labs/{name}/draft")
    async def draft_machine(
        request: Request,
        name: str,
        body: MachineBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Draft what the service enforces on a machine from its description (the
        environment and rules in plain language): an agent reads it with the current
        settings and returns the run folder, setup, time limit, refused commands, and
        GPU thresholds, plus the rules only the reproduction agent can follow. Nothing
        is saved: the page shows the draft and its diff, and Save applies it."""
        service = service_of(request)
        if not (await service.service_gate(user.login)).allowed:
            await _require_maintainer(service, user)
        machine = _connected(body)
        route = service.settings.routing.get("chat")
        if route is None:
            raise HTTPException(500, "no agent route for drafting settings")
        current = {
            "workdir": machine.workdir,
            "setup": machine.setup,
            "timeout_minutes": machine.timeout_seconds // 60,
            "forbid": machine.forbid,
            "max_gpu_memory_mib": machine.max_gpu_memory_mib,
            "max_gpu_util_pct": machine.max_gpu_util_pct,
        }
        workdir = service.settings.scratch_dir / "service" / "machines"
        workdir.mkdir(parents=True, exist_ok=True)
        prompt = "\n\n".join(
            [
                DRAFT.format(name=name),
                "Current settings:\n"
                + json.dumps(current, ensure_ascii=False, indent=1),
                "Description:\n" + (machine.description.strip() or "(empty)"),
                schema_hint(MachineRules.model_json_schema()),
            ]
        )
        result = await service.a.agents.run(
            AgentTask(
                kind=TaskKind.CHAT,
                prompt=prompt,
                workdir=workdir,
                mode="read_only",
                output_schema=MachineRules.model_json_schema(),
                job_id=new_id("run"),
                requested_by=user.login,
                untrusted_input=False,
            ),
            route,
            MachineRules,
        )
        if not result.ok or result.structured is None:
            raise HTTPException(502, f"the agent could not draft it: {result.error}")
        rules = MachineRules.model_validate(result.structured)
        drafted = machine.model_copy(
            update={
                "workdir": rules.workdir.strip() or machine.workdir,
                "setup": rules.setup.strip(),
                "timeout_seconds": rules.timeout_minutes * 60,
                "forbid": [p for p in (x.strip() for x in rules.forbid) if p],
                "max_gpu_memory_mib": rules.max_gpu_memory_mib,
                "max_gpu_util_pct": rules.max_gpu_util_pct,
            }
        )
        text, _ = await service.settings_text()
        return {
            "machine": drafted.model_dump(mode="json"),
            "left_to_agent": rules.left_to_agent,
            "problems": machine_problems(name, drafted),
            "diff": settings_diff(text, _with_machine(text, name, drafted)),
        }

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
        if machine is not None and machine.owner:
            if not owns(machine, user.login):
                machine = None  # someone's own machine is theirs alone to check
        elif not (await service.service_gate(user.login)).allowed:
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
        """List a saved provider's models with its stored key; spends no tokens."""
        service = service_of(request)
        await _require_maintainer(service, user)
        provider = service.service_config.providers.get(provider_id)
        if provider is None:
            raise HTTPException(404, f"no provider named {provider_id!r} is in force")
        key = await _stored_key(service, provider_id)
        if provider.api_key and not key:
            return {
                "ok": False,
                "error": f"the key {provider.api_key} is not set yet",
                "models": [],
            }
        return await list_models(provider.protocol, provider.base_url, key)

    @r.put("/providers/{provider_id}")
    async def put_provider(
        request: Request,
        provider_id: str,
        body: ApiForm,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Add or change a model provider from its form: a settings change like any
        other, which the owner's applies at once and anyone else's proposes. A key
        typed into the form goes into the secret store under ``api_key``
        (``<ID>_API_KEY`` unless named), which only the owner may do; it is never
        written to the settings or sent back."""
        service = service_of(request)
        if not PROVIDER_ID.match(provider_id):
            raise HTTPException(
                422, "ids are lowercase words joined by hyphens, such as my-gateway"
            )
        text, _ = await service.settings_text()
        current = _settings_data(text).get("providers") or {}
        secret = (
            body.api_key
            or (current.get(provider_id) or {}).get("api_key")
            or (
                f"{provider_id.upper().replace('-', '_')}_API_KEY" if body.key else None
            )
        )
        try:
            provider = body.model_copy(update={"api_key": secret}).provider()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        draft = _with_provider(text, provider_id, provider)
        try:
            await service.check_settings_text(draft)
        except SettingsError as e:
            raise HTTPException(
                422, {"message": "the settings do not check", "problems": e.problems}
            ) from e
        if body.key:
            await _require_maintainer(service, user)
            if service.a.secrets is None:
                raise HTTPException(409, "this service has no secret store")
            await service.a.secrets.set(str(secret), body.key.strip(), user.login)
            await service.store.add_audit(
                AuditEntry(
                    kind="config", actor=user.login, summary=f"set secret {secret}"
                )
            )
        if yaml.safe_load(draft) == yaml.safe_load(text or "{}"):
            await service.reload_settings()
            return {
                "change": None,
                "approved": True,
                "diff": "",
                "status": service.settings_status.model_dump(mode="json"),
            }
        return await save(
            request,
            DocumentBody(
                yaml=draft,
                title=f"settings: {'change' if provider_id in current else 'add'} "
                f"provider {provider_id}",
            ),
            user,
        )

    @r.delete("/providers/{provider_id}")
    async def delete_provider(
        request: Request, provider_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Remove a provider: a settings change, refused while an agent uses it. Its
        key stays in the secret store."""
        service = service_of(request)
        text, _ = await service.settings_text()
        if provider_id not in (_settings_data(text).get("providers") or {}):
            raise HTTPException(404, f"the settings name no provider {provider_id!r}")
        return await save(
            request,
            DocumentBody(
                yaml=_with_provider(text, provider_id, None),
                title=f"settings: remove provider {provider_id}",
            ),
            user,
        )

    @r.post("/providers/query")
    async def query_provider(
        request: Request, body: ProbeBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The models an endpoint lists, with what it says of each, before or after
        its provider is saved; spends no tokens. The owner's alone, since it may use
        the service's stored keys."""
        service = service_of(request)
        await _require_maintainer(service, user)
        key = body.key or await _stored_key(service, body.provider)
        return await list_models(body.protocol, body.base_url, key)

    @r.post("/providers/examine")
    async def examine_provider(
        request: Request, body: ProbeBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Talk to one model to learn what it can do (``model_apis.examine``), and
        which agents can run on it; spends a few thousand tokens. The owner's alone."""
        service = service_of(request)
        await _require_maintainer(service, user)
        if not body.model:
            raise HTTPException(422, "name the model to examine")
        key = body.key or await _stored_key(service, body.provider)
        return await examined(body, key)

    return r


async def _stored_key(service: Service, provider_id: str | None) -> str | None:
    """The stored key of the service's provider ``provider_id``, if any."""
    provider = service.service_config.providers.get(provider_id or "")
    if provider is None or not provider.api_key or service.a.secrets is None:
        return None
    return await service.a.secrets.get(provider.api_key)


async def examined(body: ProbeBody, key: str | None) -> dict[str, Any]:
    """``examine`` of ``body.model``, with the agents that can run on it."""
    out = await examine(body.base_url, key, str(body.model), body.protocol)
    spec = ModelSpec.model_validate(out["spec"])
    out["agents"] = agents_for(spec, body.protocol) if out["ok"] else []
    return out
