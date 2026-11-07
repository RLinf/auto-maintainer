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
import shlex
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
MACHINE_NAME = PROVIDER_ID
SSH_DESTINATION = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$")
"""``user@host`` or a ``Host`` alias from the service user's SSH config. Never an
option: a destination that starts with ``-`` would be read by ssh as one."""

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
    effort: str | None = None
    """The reasoning level when a route names none; unset keeps the CLI's own."""
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
    permissions: Literal["auto", "restricted", "bypass"] | None = None
    """The mode this agent runs every task in, instead of the task route's. ``None``
    (the default) follows the route. ``bypass`` is itself the opt-in, so it needs no
    ``allow_bypass``, but it still applies only to input from the repository's team
    unless the route sets ``bypass_untrusted``; other input runs in ``auto``."""
    command_prefix: list[str] = Field(default_factory=list)
    """A wrapper put before the command, such as a sandbox (``bwrap ... --``) or a
    switch to an unprivileged user. ``{workdir}`` and ``{run_dir}`` are filled in."""
    approve_commands: list[str] = Field(default_factory=list)
    """Commands the service approves for this agent without asking a person, in any
    task and on any PR, beyond its own rules for reading (agents the service answers
    for: the ACP CLIs, such as Antigravity). Each is a command's leading words, such
    as ``python3 .agents/skills/docs-check/check_rst_markup.py``; it matches that
    command with any further arguments while the paths among them stay in the run's
    folders. Only for commands that cannot run a PR's code or reach outside its
    checkout, such as a repository's own read-only scripts, which come from its
    default branch; ``approve_command_problems`` refuses an interpreter or a shell
    without a script and the runners that run a project's code."""
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


class LabMachine(BaseModel):
    """A machine where the service runs a PR's code to reproduce what it claims
    (design 5.10). Shared by every repository unless ``repos`` names some."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    """The machine's environment and rules in plain language: its hardware, the
    environment runs use and how to set it up, where data lives, what runs must not
    do, how long they may take. The reproduction agent reads it as written; the
    settings after ``repos`` are drafted from it (``web/settings.py``, the draft
    endpoint) and are what the service enforces."""
    transport: Literal["ssh", "local"] = "ssh"
    host: str | None = None
    """The host to reach (``HostName``: an address, a name, or an alias from the
    service user's SSH config, which may hold the rest); defaults to the machine's
    name. ``user@host`` is accepted too."""
    user: str | None = None
    """The login on the machine (``User``)."""
    port: int | None = Field(default=None, ge=1, le=65535)
    """The SSH port (``Port``)."""
    proxy_jump: str | None = None
    """Hosts to go through first (``ProxyJump``): ``[user@]host[:port]``, several
    separated by commas."""
    identity_file: str | None = None
    """The private key on the service's host (``IdentityFile``), such as
    ``~/.ssh/id_ed25519``; without one, the service user's SSH agent and config."""
    repos: list[str] = Field(default_factory=list)
    """Repositories that may use the machine; empty means all."""
    workdir: str = "am-labs"
    """Where runs go on the machine, relative to the login's home unless absolute.
    Each run gets its own folder there, removed afterwards."""
    setup: str = ""
    """Shell run before every reproduction, such as activating an environment."""
    forbid: list[str] = Field(default_factory=list)
    """Regular expressions a script must not match, besides the built-in ones."""
    timeout_seconds: int = Field(default=1800, ge=60, le=6 * 3600)
    max_gpu_memory_mib: int = 500
    """A GPU with less memory in use counts as idle (and none of its processes)."""
    max_gpu_util_pct: int = 0

    def serves(self, repo: str) -> bool:
        return not self.repos or repo in self.repos


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
    default_agent: str | None = None
    """The agent every task kind tries first; unset keeps the one chosen during
    setup. See ``effective_routing``."""
    agents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per agent, the fields that differ from its defaults (``RuntimeConfig``)."""
    providers: dict[str, Provider] = Field(default_factory=dict)
    routing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per task kind, the route fields that differ from the code's defaults. A
    repository's own profile can still override them for that repository."""
    system: SystemSettings = Field(default_factory=SystemSettings)
    labs: dict[str, LabMachine] = Field(default_factory=dict)
    """Test machines by name (design 5.10)."""


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


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
"""Reasoning levels an agent's ``effort`` may name; each adapter maps them to its
CLI's own (the Settings page offers only the levels the CLI reports)."""

KEEPS_OWN_PRIMARY = frozenset({"verify"})
"""Task kinds whose default route ignores the default agent: verification looks for
a second opinion from another agent, and reaches the default agent through its
fallbacks when no other is installed."""


def effective_routing(
    defaults: Mapping[str, Route],
    config: ServiceConfig,
    host_default_agent: str | None = None,
) -> dict[str, Route]:
    """The code's default routes, led by the default agent, with the settings' route
    changes laid over them.

    The default agent is ``config.default_agent``, else ``host_default_agent`` (the
    one chosen during setup). It becomes the primary of every default route except
    those in ``KEEPS_OWN_PRIMARY``, and the route's own primary its first fallback.
    A route in ``config.routing``, and a repository's profile, still win.
    """
    out = {name: route.model_copy(deep=True) for name, route in defaults.items()}
    agent = config.default_agent or host_default_agent
    if agent:
        for task, route in out.items():
            if task in KEEPS_OWN_PRIMARY or route.primary == agent:
                continue
            order = [route.primary, *route.fallback]
            route.fallback = [name for name in order if name != agent]
            route.primary = agent
            route.model = None
    for task, changes in config.routing.items():
        base = out[task].model_dump() if task in out else {}
        out[task] = Route.model_validate({**base, **changes})
    return out


_INTERPRETERS = frozenset(
    {"python", "python3", "bash", "sh", "zsh", "node", "perl", "ruby", "deno", "bun"}
)
_RUNNERS = frozenset(
    {
        "uv", "uvx", "pip", "pip3", "npm", "npx", "yarn", "pnpm", "make", "cargo",
        "go", "pytest", "tox", "nox", "sudo", "env", "xargs", "timeout", "nice",
        "docker", "kubectl", "ssh", "curl", "wget", "git",
    }
)  # fmt: skip
"""Commands that run a project's code, reach the network, or run another command,
whatever follows them: never approved by prefix. Git has its own read rules."""


def approve_command_problems(command: str) -> list[str]:
    """Why ``command`` may not be approved without asking; empty when it may."""
    if any(ch in command for ch in (";", "&", "|", ">", "<", "`", "$(", "\n")):
        return ["it has shell syntax; list a single command"]
    try:
        words = shlex.split(command)
    except ValueError as e:
        return [f"it does not parse: {e}"]
    if not words:
        return ["it is empty"]
    name = words[0].rsplit("/", 1)[-1]
    if name in _RUNNERS:
        return [f"{name} runs other code or reaches outside the checkout"]
    if name in _INTERPRETERS and (len(words) < 2 or words[1].startswith("-")):
        return [f"{name} needs the script it runs, such as {name} path/to/script"]
    return []


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
        if agent.permissions == "bypass" and not can_bypass(agent):
            problems.append(
                f"agents.{name}: this CLI has no bypass mode; for the generic "
                "adapter, set bypass_args"
            )
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
    for name, machine in config.labs.items():
        problems += [f"labs.{name}: {p}" for p in machine_problems(name, machine)]
    for name, agent in agents.items():
        for i, command in enumerate(agent.approve_commands):
            problems += [
                f"agents.{name}.approve_commands.{i} ({command!r}): {p}"
                for p in approve_command_problems(command)
            ]
        if agent.effort is not None and agent.effort not in EFFORTS:
            problems.append(
                f"agents.{name}.effort: use one of {', '.join(EFFORTS)}, not {agent.effort!r}"
            )
    if config.default_agent is not None:
        if config.default_agent not in agents:
            problems.append(
                f"default_agent: there is no agent named {config.default_agent!r}"
            )
        elif not agents[config.default_agent].enabled:
            problems.append(
                f"default_agent: agent {config.default_agent!r} is disabled"
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


SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
SSH_JUMP = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?$"
)
SSH_KEY_PATH = re.compile(r"^[~/A-Za-z0-9._][~/A-Za-z0-9._+@-]*$")
SSH_KEYWORDS = {
    "hostname": "host",
    "user": "user",
    "port": "port",
    "proxyjump": "proxy_jump",
    "identityfile": "identity_file",
}
"""The ``~/.ssh/config`` keywords a machine's connection may use, and their fields.
Others are refused: ``ProxyCommand``, ``LocalCommand``, ``KnownHostsCommand``, and
``Match exec`` run commands on the service's host."""
_SSH_LINE = re.compile(r"^(\S+?)(?:\s*=\s*|\s+)(.*)$")


def parse_ssh_config(text: str) -> tuple[dict[str, Any], list[str]]:
    """The connection fields of an ``~/.ssh/config``-style entry for one machine
    (``Host`` names it and is otherwise ignored; ``HostName``, ``User``, ``Port``,
    ``ProxyJump``, ``IdentityFile``), and the problems that keep it from being used."""
    fields: dict[str, Any] = {}
    problems: list[str] = []
    hosts = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SSH_LINE.match(line)
        if match is None:
            problems.append(f"{line!r}: write a keyword and its value")
            continue
        keyword, value = match.group(1), match.group(2).strip().strip('"')
        if keyword.lower() == "host":
            hosts += 1
            if hosts > 1:
                problems.append("write one Host entry per machine")
            continue
        field_name = SSH_KEYWORDS.get(keyword.lower())
        if field_name is None:
            problems.append(
                f"{keyword} is not supported: a machine uses HostName, User, Port, "
                "ProxyJump, and IdentityFile"
            )
            continue
        if not value:
            problems.append(f"{keyword} needs a value")
            continue
        if field_name == "port":
            if not value.isdigit() or not 0 < int(value) < 65536:
                problems.append(f"Port {value!r} is not a port number")
                continue
            fields["port"] = int(value)
        else:
            fields[field_name] = value
    return fields, problems


def ssh_config_text(name: str, machine: LabMachine) -> str:
    """``machine``'s connection as an ``~/.ssh/config`` entry, for editing."""
    lines = [f"Host {name}"]
    for keyword, value in (
        ("HostName", machine.host),
        ("User", machine.user),
        ("Port", machine.port),
        ("ProxyJump", machine.proxy_jump),
        ("IdentityFile", machine.identity_file),
    ):
        if value:
            lines.append(f"  {keyword} {value}")
    return "\n".join(lines)


def machine_problems(name: str, machine: LabMachine) -> list[str]:
    """Why ``machine`` cannot be used as configured."""
    out = []
    if not MACHINE_NAME.match(name):
        out.append("names are lowercase words joined by hyphens, such as gpu-01")
    if machine.transport == "ssh" and not SSH_DESTINATION.match(machine.host or name):
        out.append(
            "HostName must be an address, a host name, or an alias (user@host also "
            "works), without spaces or a leading '-'"
        )
    if machine.transport == "ssh":
        if machine.user is not None and not SSH_USER.match(machine.user):
            out.append(f"User {machine.user!r} is not a login name")
        jumps = (machine.proxy_jump or "").split(",") if machine.proxy_jump else []
        for jump in jumps:
            if not SSH_JUMP.match(jump.strip()):
                out.append(
                    f"ProxyJump {jump.strip()!r} must be [user@]host[:port], several "
                    "separated by commas"
                )
        if machine.identity_file is not None and not SSH_KEY_PATH.match(
            machine.identity_file
        ):
            out.append(
                f"IdentityFile {machine.identity_file!r} must be a path on the "
                "service's host, without spaces"
            )
    workdir = machine.workdir.strip()
    if not workdir or ".." in workdir.split("/") or "\n" in workdir:
        out.append("workdir must be a plain path without '..'")
    elif workdir.rstrip("/") in ("", "~", "/", "/tmp", "/home", "/root"):
        out.append("workdir must be a folder of its own, not / or a home or /tmp")
    for pattern in machine.forbid:
        try:
            re.compile(pattern)
        except re.error as e:
            out.append(f"forbid pattern {pattern!r} is not a regular expression ({e})")
    for repo in machine.repos:
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            out.append(f"repos: {repo!r} is not owner/name")
    return out


def can_bypass(agent: RuntimeConfig) -> bool:
    """Whether the CLI has a mode that skips its permission checks."""
    return agent.adapter != "generic" or bool(agent.bypass_args)


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


class UserToken(BaseModel):
    """A maintainer's own GitHub token, kept on the host so the reviews they approve
    can be posted as them. Never shown, logged, or put in a prompt."""

    token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    """Only on tokens kept from the removed GitHub App sign-in, which expire."""
    expires_at: datetime | None = None
    source: Literal["oauth", "device", "gh", "token"] = "gh"
    """How the person signed in: ``gh``; the others name removed sign-ins whose
    tokens may still be in ``user-tokens.json``."""
    updated_at: datetime | None = None
