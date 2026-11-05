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

"""Runtime configuration (operators' ``runtimes.yaml``) and building runtimes from it."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.ports import AgentRuntime
from .adapters.antigravity import AntigravityRuntime
from .adapters.claude_code import ClaudeCodeRuntime
from .adapters.codex import CodexRuntime
from .adapters.gemini import GeminiRuntime
from .adapters.generic import GenericRuntime
from .adapters.opencode import OpenCodeRuntime
from .exec import ExecRuntime

AdapterName = Literal[
    "claude-code", "codex", "antigravity", "opencode", "gemini", "generic"
]

ADAPTERS: dict[str, type[ExecRuntime]] = {
    "claude-code": ClaudeCodeRuntime,
    "codex": CodexRuntime,
    "antigravity": AntigravityRuntime,
    "opencode": OpenCodeRuntime,
    "gemini": GeminiRuntime,
    "generic": GenericRuntime,
}


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = None
    daily_runs: int | None = None


class GenericOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["text", "json", "jsonl"] = "text"
    final_path: str | None = None


class RuntimeConfig(BaseModel):
    """One runtime as operators declare it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    adapter: AdapterName
    binary: str | None = None
    model: str | None = None
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
lines are unconfirmed, so they stay disabled until an operator sets them."""


def merge_configs(
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, RuntimeConfig]:
    """Defaults overlaid with operator overrides, key by key for known runtimes."""
    merged = {name: cfg.model_copy(deep=True) for name, cfg in DEFAULT_RUNTIMES.items()}
    for name, data in (overrides or {}).items():
        base = merged[name].model_dump() if name in merged else {}
        merged[name] = RuntimeConfig.model_validate({**base, **dict(data)})
    return merged


def build_runtime(
    name: str, config: RuntimeConfig, runs_dir: Path | str | None = None
) -> AgentRuntime:
    """Build one runtime from its configuration."""
    cls = ADAPTERS[config.adapter]
    kwargs: dict[str, Any] = {
        "runs_dir": runs_dir,
        "env_allow": config.env_allow,
        "model": config.model,
        "extra_args": config.extra_args,
        "allow_bypass": config.allow_bypass,
        "command_prefix": config.command_prefix,
        **config.options,
    }
    if config.binary:
        kwargs["binary"] = config.binary
    if cls is GenericRuntime:
        if not config.command:
            raise ValueError(
                f"runtime {name!r} uses the generic adapter but has no command"
            )
        return GenericRuntime(
            name,
            command=config.command,
            output_format=config.output.format,
            final_path=config.output.final_path,
            bypass_args=config.bypass_args,
            **kwargs,
        )
    return cls(name, **kwargs)


def build_runtimes(
    configs: Mapping[str, RuntimeConfig], runs_dir: Path | str | None = None
) -> dict[str, AgentRuntime]:
    """Build every enabled runtime, keyed by name."""
    return {
        name: build_runtime(name, cfg, runs_dir)
        for name, cfg in configs.items()
        if cfg.enabled
    }


def budgets_of(configs: Mapping[str, RuntimeConfig]) -> dict[str, Budget]:
    """The budget of each runtime, for the router."""
    return {name: cfg.budget for name, cfg in configs.items()}
