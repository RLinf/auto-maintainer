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

"""OpenCode (``opencode acp``), checked against version 2.0.32.

``opencode run --auto`` approves everything that is not denied and cannot ask the
caller, so the service runs ``opencode acp`` (see ``agents.acp``). Its ACP server has
no modes; which tools ask first comes from OpenCode's ``permission`` config, passed
inline in ``OPENCODE_CONFIG_CONTENT``. Outside bypass mode edits, shell commands, web
fetches, and paths outside the worktree all ask, so the automatic rules and reviewers
see each one; read-only tasks deny edits outright. The model is the session config
option ``model``. On this host the handshake passed; prompts could not be run because
the account had no quota left, so the permission flow follows the ACP specification.
"""

from __future__ import annotations

import json
from typing import Any

from ...domain.agents import AgentTask
from ..acp import AcpRuntime
from ..exec import RunFiles


class OpenCodeRuntime(AcpRuntime):
    """OpenCode's ACP server."""

    default_name = "opencode"
    default_binary = "opencode"
    default_env_allow = (
        "OPENCODE_*",
        "ANTHROPIC_*",
        "OPENAI_*",
        "GEMINI_*",
        "OPENROUTER_*",
    )

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        return [binary, "acp"]

    def task_env(self, task: AgentTask) -> dict[str, str]:
        if task.permissions == "bypass":
            permission: dict[str, str] = {
                "edit": "allow",
                "bash": "allow",
                "webfetch": "allow",
                "external_directory": "allow",
            }
        else:
            permission = {
                "edit": "deny" if task.mode == "read_only" else "ask",
                "bash": "ask",
                "webfetch": "ask",
                "external_directory": "ask",
            }
        config: dict[str, Any] = {"permission": permission}
        if self.provider is not None:
            config["provider"] = {self._provider_id(): self._provider_config()}
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}

    def _provider_id(self) -> str:
        assert self.provider is not None
        return f"am-{self.provider.id}"

    def _provider_config(self) -> dict[str, Any]:
        """OpenCode's provider entry; the key is an ``{env:...}`` reference, not the value."""
        p = self.provider
        assert p is not None
        npm = {
            "anthropic": "@ai-sdk/anthropic",
            "openai": "@ai-sdk/openai-compatible",
            "openai-responses": "@ai-sdk/openai",
        }[p.protocol]
        options: dict[str, Any] = {"baseURL": p.base_url}
        if p.api_key:
            options["apiKey"] = "{env:AM_PROVIDER_API_KEY}"
        models = [
            *p.models,
            *([self.model] if self.model and self.model not in p.models else []),
        ]
        return {
            "npm": npm,
            "name": p.name or p.id,
            "options": options,
            "models": {m: {"name": m} for m in models},
        }

    def provider_env(self) -> dict[str, str | None]:
        if self.provider is None or not self.provider.api_key:
            return {}
        return {"AM_PROVIDER_API_KEY": self.provider.api_key}

    def prompt_text(self, task: AgentTask) -> str:
        dirs = [str(d) for d in [*task.skill_dirs, *task.extra_dirs]]
        if not dirs:
            return task.prompt
        listing = "\n".join(f"- {d}" for d in dirs)
        return (
            "Skills and reference material for this task are in these directories; read "
            f"the SKILL.md files there when they apply:\n{listing}\n\n{task.prompt}"
        )

    def session_setup(
        self, task: AgentTask, session: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        model = task.model or self.model
        if model and self.provider is not None and "/" not in model:
            model = f"{self._provider_id()}/{model}"
        if model:
            return [
                ("session/set_config_option", {"configId": "model", "value": model})
            ]
        return []
