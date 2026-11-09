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

"""Building runtimes from their settings (``domain.settings``)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain.ports import AgentRuntime
from ..domain.settings import (
    DEFAULT_RUNTIMES,
    AdapterName,
    Budget,
    GenericOutput,
    ProviderBinding,
    RuntimeConfig,
    merge_configs,
)
from .adapters.antigravity import AntigravityRuntime
from .adapters.claude_code import ClaudeCodeRuntime
from .adapters.codex import CodexRuntime
from .adapters.dsh import DshRuntime
from .adapters.gemini import GeminiRuntime
from .adapters.generic import GenericRuntime
from .adapters.opencode import OpenCodeRuntime
from .exec import ExecRuntime
from .sandbox import register_secret

__all__ = [
    "ADAPTERS",
    "DEFAULT_RUNTIMES",
    "AdapterName",
    "Budget",
    "GenericOutput",
    "RuntimeConfig",
    "budgets_of",
    "build_runtime",
    "build_runtimes",
    "merge_configs",
]

ADAPTERS: dict[str, type[ExecRuntime]] = {
    "claude-code": ClaudeCodeRuntime,
    "codex": CodexRuntime,
    "antigravity": AntigravityRuntime,
    "opencode": OpenCodeRuntime,
    "dsh": DshRuntime,
    "gemini": GeminiRuntime,
    "generic": GenericRuntime,
}


def build_runtime(
    name: str,
    config: RuntimeConfig,
    runs_dir: Path | str | None = None,
    provider: ProviderBinding | None = None,
) -> AgentRuntime:
    """Build one runtime from its configuration.

    ``provider`` points the CLI at a model endpoint instead of its own sign-in; its key
    is registered for scrubbing, so transcripts and errors never carry it.
    """
    cls = ADAPTERS[config.adapter]
    if provider is not None and provider.api_key:
        register_secret(provider.api_key)
    kwargs: dict[str, Any] = {
        "runs_dir": runs_dir,
        "provider": provider,
        "env_allow": config.env_allow,
        "model": config.model,
        "effort": config.effort,
        "extra_args": config.extra_args,
        "allow_bypass": config.allow_bypass,
        "permissions": config.permissions,
        "command_prefix": config.command_prefix,
        "approve_commands": config.approve_commands,
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
    configs: Mapping[str, RuntimeConfig],
    runs_dir: Path | str | None = None,
    providers: Mapping[str, ProviderBinding] | None = None,
) -> dict[str, AgentRuntime]:
    """Build every enabled runtime, keyed by name, each with its provider if it names one."""
    return {
        name: build_runtime(
            name,
            cfg,
            runs_dir,
            (providers or {}).get(cfg.provider) if cfg.provider else None,
        )
        for name, cfg in configs.items()
        if cfg.enabled
    }


def budgets_of(configs: Mapping[str, RuntimeConfig]) -> dict[str, Budget]:
    """The budget of each runtime, for the router."""
    return {name: cfg.budget for name, cfg in configs.items()}
