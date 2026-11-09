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

"""DeepSeek Harness (``dsh --profile acp``), checked against 0.2.2-rc.1 from npm
(``@deepseek-ai/dsh``) under Node 26.7; under Node 24.16 it does not start, because
its native addon loader fails.

dsh has no sign-in (its ACP server lists no auth methods): it runs on a model
provider. ``DEEPSEEK_API_KEY``, and ``DEEPSEEK_BASE_URL`` for a gateway that speaks
the OpenAI Chat Completions API, come from the agent's provider in the service
settings, or from the person who asked when they set their own. Its sessions offer
the config options ``model`` and ``reasoning_effort`` (off, low, high, max) and no
modes; what it would ask goes through ``session/request_permission`` as the ACP
specification says, answered by the service's automatic rules. On this host the
handshake and ``session/new`` passed; prompts were not run, for want of a key.
``DSH_HOME`` is ``~/.dsh``: its sessions and settings, mounted into containers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.agents import AgentTask
from ..acp import AcpRuntime
from ..exec import RunFiles

_LEVELS = {"low": "low", "medium": "high", "high": "high", "xhigh": "max", "max": "max"}


class DshRuntime(AcpRuntime):
    """DeepSeek Harness's ACP server."""

    home_paths = (".dsh",)
    default_name = "dsh"
    default_binary = "dsh"
    default_env_allow = ("DSH_*", "DEEPSEEK_*")

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        return [binary, "--profile", "acp"]

    def task_env(self, task: AgentTask) -> dict[str, str]:
        return {"DSH_HOME": str(Path.home() / ".dsh")}

    def provider_env(self) -> dict[str, str | None]:
        p = self.provider
        if p is None:
            return {}
        return {
            "DEEPSEEK_API_KEY": p.api_key or None,
            "DEEPSEEK_BASE_URL": p.base_url or None,
        }

    def session_setup(
        self, task: AgentTask, session: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        model = task.model or self.model
        if model:
            calls.append(
                ("session/set_config_option", {"configId": "model", "value": model})
            )
        if task.effort:
            level = _LEVELS.get(task.effort, task.effort)
            calls.append(
                (
                    "session/set_config_option",
                    {"configId": "reasoning_effort", "value": level},
                )
            )
        return calls
