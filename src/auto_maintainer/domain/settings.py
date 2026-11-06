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

"""Service settings (design 8.1): which agents run, which model providers they use,
the default route per task, and a few system values.

They live in one document, ``config/service.yaml`` in the memory repo, which only
maintainers of the service's repository change (design 2.3). API keys never appear in
it: a provider names a secret, and the secret's value stays on the host
(``SecretStore``). Everything here is pure: parsing, merging over the defaults, and
checking that the pieces fit together.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .agents import TaskKind
from .profile import Route

SETTINGS_PATH = "config/service.yaml"
"""The settings document in the memory repo."""
LEGACY_RUNTIMES_PATH = "config/runtimes.yaml"
"""Where agents were declared before ``service.yaml``; still read, and overridden by it."""
SETTINGS_SCHEMA = 1

SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
PROVIDER_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

AdapterName = Literal[
    "claude-code", "codex", "antigravity", "opencode", "gemini", "generic"
]
ProviderProtocol = Literal["anthropic", "openai", "openai-responses"]
"""The wire API a provider speaks: Anthropic Messages, OpenAI Chat Completions, or
OpenAI Responses."""

PROVIDER_PROTOCOLS: dict[str, frozenset[str]] = {
    "claude-code": frozenset({"anthropic"}),
    "codex": frozenset({"openai", "openai-responses"}),
    "opencode": frozenset({"anthropic", "openai", "openai-responses"}),
    "generic": frozenset({"anthropic", "openai", "openai-responses"}),
    "gemini": frozenset(),
    "antigravity": frozenset(),
}
"""Which provider protocols each adapter can point its CLI at. Gemini CLI and
Antigravity only talk to their own services."""


class SettingsError(ValueError):
    """The settings document cannot be used; ``problems`` says why, one line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# -- agents -----------------------------------------------------------------------


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = None
    daily_runs: int | None = None


class GenericOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["text", "json", "jsonl"] = "text"
    final_path: str | None = None


class RuntimeConfig(BaseModel):
    """One agent CLI as the service runs it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    adapter: AdapterName
    binary: str | None = None
    model: str | None = None
    """The model when a route names none; with a provider, its first model by default."""
    provider: str | None = None
    """A provider in ``providers``; without one the CLI uses its own sign-in."""
    env_allow: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    options: dict[str, Any] = Field(default_factory=dict)
    """Adapter-specific keyword arguments, such as ``setting_sources`` for Claude Code."""
    allow_bypass: bool = False
    """Whether routes that ask for bypass mode may run this CLI with every permission
    check skipped. Off by default; without it such routes run in ``auto`` mode."""
    command_prefix: list[str] = Field(default_factory=list)
    """A wrapper put before the command, such as a sandbox (``bwrap ... --``) or a
    switch to an unprivileged user. ``{workdir}`` and ``{run_dir}`` are filled in."""
    bypass_args: list[str] = Field(default_factory=list)
    """For the generic adapter: arguments that switch the CLI into its bypass mode."""
    command: list[str] = Field(default_factory=list)
    """For the generic adapter: the command template."""
    output: GenericOutput = Field(default_factory=GenericOutput)


DEFAULT_RUNTIMES: dict[str, RuntimeConfig] = {
    "claude-code": RuntimeConfig(adapter="claude-code"),
    "codex": RuntimeConfig(adapter="codex"),
    "antigravity": RuntimeConfig(adapter="antigravity"),
    "opencode": RuntimeConfig(adapter="opencode"),
    "gemini": RuntimeConfig(adapter="gemini"),
    "pi": RuntimeConfig(
        adapter="generic",
        enabled=False,
        command=["pi", "-p", "{prompt}"],
        output=GenericOutput(format="text"),
    ),
    "dsh": RuntimeConfig(
        adapter="generic",
        enabled=False,
        command=["dsh", "run", "--cwd", "{workdir}", "--prompt-file", "{prompt_file}"],
        output=GenericOutput(format="text"),
    ),
}
"""Defaults for every known CLI. ``pi`` and ``dsh`` are placeholders: their command
lines are unconfirmed, so they stay disabled until someone sets them."""


def merge_configs(
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, RuntimeConfig]:
    """Defaults overlaid with overrides, key by key for known runtimes."""
    merged = {name: cfg.model_copy(deep=True) for name, cfg in DEFAULT_RUNTIMES.items()}
    for name, data in (overrides or {}).items():
        base = merged[name].model_dump() if name in merged else {}
        merged[name] = RuntimeConfig.model_validate({**base, **dict(data)})
    return merged


# -- providers ---------------------------------------------------------------------


class Provider(BaseModel):
    """A model endpoint the agents' CLIs can use instead of their own sign-in."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    protocol: ProviderProtocol
    base_url: str
    api_key: str | None = None
    """The name of the secret that holds the key, such as ``DEEPSEEK_API_KEY``; never
    the key itself."""
    models: list[str] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/@]+(/\S*)?$", v):
            raise ValueError("must be an http(s) URL without credentials in it")
        return v

    @field_validator("api_key")
    @classmethod
    def _secret_name(cls, v: str | None) -> str | None:
        if v is not None and not SECRET_NAME.match(v):
            raise ValueError(
                "must name a secret in capitals, such as DEEPSEEK_API_KEY; "
                "the key itself is set separately and never written here"
            )
        return v


@dataclass(frozen=True)
class ProviderBinding:
    """A provider with its key, handed to one runtime when it is built."""

    id: str
    name: str
    protocol: str
    base_url: str
    models: tuple[str, ...] = ()
    api_key: str | None = field(default=None, repr=False)


# -- system ------------------------------------------------------------------------


class SystemSettings(BaseModel):
    """Service-wide values; each unset one keeps the host's value."""

    model_config = ConfigDict(extra="forbid")

    poll_seconds: int | None = Field(default=None, ge=30, le=3600)
    """How often the service checks every repository for new activity."""
    max_public_writes_per_hour: int | None = Field(default=None, ge=0, le=2000)
    """Most public writes (comments, labels, reviews) across all repositories per hour."""
    max_repo_public_writes_per_hour: int | None = Field(default=None, ge=0, le=500)
    """Most public writes on one repository per hour."""


# -- the document ------------------------------------------------------------------


class ServiceConfig(BaseModel):
    """``config/service.yaml``. Every section is optional and only says what differs
    from the defaults."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(default=SETTINGS_SCHEMA, alias="schema")
    agents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per agent, the fields that differ from its defaults (``RuntimeConfig``)."""
    providers: dict[str, Provider] = Field(default_factory=dict)
    routing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per task kind, the route fields that differ from the code's defaults. A
    repository's own profile can still override them for that repository."""
    system: SystemSettings = Field(default_factory=SystemSettings)


class SettingsStatus(BaseModel):
    """Whether the settings in the memory repo are what the service runs."""

    applied: bool = True
    problems: list[str] = Field(default_factory=list)
    """Why the newest document was not applied; the previous settings stay in force."""
    loaded_at: datetime | None = None


class SecretInfo(BaseModel):
    """What the dashboard may know about a secret: never its value."""

    name: str
    configured: bool
    source: Literal["dashboard", "host", "missing"] = "missing"
    updated_at: datetime | None = None
    updated_by: str | None = None


def _where(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) or "settings"


def parse_settings(text: str | None) -> ServiceConfig:
    """Parse ``config/service.yaml``.

    Raises:
        SettingsError: The YAML is malformed or a field has the wrong shape.
    """
    if not text or not text.strip():
        return ServiceConfig()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SettingsError([f"not valid YAML: {e}"]) from e
    if data is None:
        return ServiceConfig()
    if not isinstance(data, dict):
        raise SettingsError(["the settings must be a mapping of sections"])
    version = data.get("schema", SETTINGS_SCHEMA)
    if version != SETTINGS_SCHEMA:
        raise SettingsError([f"unknown settings schema {version}"])
    try:
        return ServiceConfig.model_validate(data)
    except ValidationError as e:
        raise SettingsError(
            [f"{_where(err['loc'])}: {err['msg']}" for err in e.errors()]
        ) from e


def parse_legacy_runtimes(text: str | None) -> dict[str, dict[str, Any]]:
    """The ``runtimes`` mapping of the older ``config/runtimes.yaml``."""
    if not text:
        return {}
    data = yaml.safe_load(text) or {}
    runtimes = data.get("runtimes") if isinstance(data, dict) else None
    return dict(runtimes or {})


def effective_agents(
    config: ServiceConfig, legacy: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, RuntimeConfig]:
    """Every agent with its defaults, then ``runtimes.yaml``, then ``service.yaml``."""
    names = set(legacy or {}) | set(config.agents)
    overrides = {
        name: {**dict((legacy or {}).get(name, {})), **config.agents.get(name, {})}
        for name in names
    }
    return merge_configs(overrides)


def effective_routing(
    defaults: Mapping[str, Route], config: ServiceConfig
) -> dict[str, Route]:
    """The code's default routes with the settings' changes laid over them."""
    out = {name: route.model_copy(deep=True) for name, route in defaults.items()}
    for task, changes in config.routing.items():
        base = out[task].model_dump() if task in out else {}
        out[task] = Route.model_validate({**base, **changes})
    return out


def check_settings(
    config: ServiceConfig,
    defaults: Mapping[str, Route],
    legacy: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Problems that keep ``config`` from running, each as one sentence. Empty when it fits."""
    problems: list[str] = []
    try:
        agents = effective_agents(config, legacy)
    except ValidationError as e:
        return [f"agents.{_where(err['loc'])}: {err['msg']}" for err in e.errors()]
    except ValueError as e:
        return [f"agents: {e}"]
    for pid in config.providers:
        if not PROVIDER_ID.match(pid):
            problems.append(
                f"providers.{pid}: ids are lowercase words joined by hyphens, such as my-gateway"
            )
    for name, agent in agents.items():
        if agent.adapter == "generic" and agent.enabled and not agent.command:
            problems.append(f"agents.{name}: the generic adapter needs a command")
        if agent.provider is None:
            continue
        provider = config.providers.get(agent.provider)
        if provider is None:
            problems.append(
                f"agents.{name}.provider: there is no provider named {agent.provider!r}"
            )
            continue
        allowed = PROVIDER_PROTOCOLS.get(agent.adapter, frozenset())
        if provider.protocol not in allowed:
            can = ", ".join(sorted(allowed)) or "no custom provider"
            problems.append(
                f"agents.{name}: {agent.adapter} cannot use a {provider.protocol} "
                f"provider (it supports {can})"
            )
        if agent.model is None and not provider.models:
            problems.append(
                f"agents.{name}: name a model, or list models on provider {agent.provider!r}"
            )
    known_tasks = {str(k) for k in TaskKind}
    for task in config.routing:
        if task not in known_tasks:
            problems.append(f"routing.{task}: there is no task kind with that name")
    try:
        routes = effective_routing(defaults, config)
    except ValidationError as e:
        return problems + [
            f"routing.{_where(err['loc'])}: {err['msg']}" for err in e.errors()
        ]
    for task, route in routes.items():
        if task not in config.routing:
            continue
        for role, name in [
            ("primary", route.primary),
            *[("fallback", n) for n in route.fallback],
            *([("verifier", route.verifier)] if route.verifier else []),
        ]:
            if name not in agents:
                problems.append(
                    f"routing.{task}.{role}: there is no agent named {name!r}"
                )
            elif role == "primary" and not agents[name].enabled:
                problems.append(f"routing.{task}.primary: agent {name!r} is disabled")
    return problems


def secret_names(config: ServiceConfig) -> list[str]:
    """Every secret the providers name."""
    return sorted({p.api_key for p in config.providers.values() if p.api_key})


def settings_diff(before: str | None, after: str | None) -> str:
    """A unified diff of two versions of the settings document."""
    return "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{SETTINGS_PATH}",
            tofile=f"b/{SETTINGS_PATH}",
        )
    )
