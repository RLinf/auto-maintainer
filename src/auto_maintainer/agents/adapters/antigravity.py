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

"""Antigravity (``agy acp``), checked against version 1.5.1.

``agy``'s print mode has an ``auto`` mode but no way to hand a permission prompt to
the caller, so the service runs ``agy acp`` and answers ``session/request_permission``
itself (see ``agents.acp``). The ACP server offers the modes ``default`` (asks before
edits and commands), ``accept-edits``, and ``plan``. Tasks that may write run in
``accept-edits``; read-only tasks run in ``default``, so the automatic rules and
reviewers see every edit and command. Read-only tasks also get ``--sandbox``, which
restricts terminal commands; on hosts where ``agy``'s sandbox server cannot start,
every command then fails, and operators set the runtime option ``sandbox: false``.
The model and the reasoning level are session config options (``model``,
``thought_level``). ``--dangerously-skip-permissions`` is bypass mode.
"""

from __future__ import annotations

from typing import Any

from ...domain.agents import AgentTask, Capabilities
from ..acp import AcpRuntime
from ..exec import RunFiles

_LEVELS = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class AntigravityRuntime(AcpRuntime):
    """Antigravity's ACP server.

    Args:
        sandbox: Pass ``--sandbox`` for read-only tasks.
        **kwargs: Passed to ``ExecRuntime``.
    """

    home_paths = (".gemini",)
    default_name = "antigravity"
    default_binary = "agy"
    default_env_allow = ("GEMINI_*", "GOOGLE_*", "ANTIGRAVITY_*", "AGY_*")

    def __init__(
        self, name: str | None = None, *, sandbox: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(name, **kwargs)
        self.sandbox = sandbox

    def capabilities(self) -> Capabilities:
        return super().capabilities().model_copy(update={"skill_dirs": True})

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        argv = [binary, "acp", "--disable-telemetry"]
        if task.permissions == "bypass":
            argv.append("--dangerously-skip-permissions")
        elif task.mode == "read_only" and self.sandbox:
            argv.append("--sandbox")
        for directory in [*task.skill_dirs, *task.extra_dirs]:
            argv += ["--add-dir", str(directory)]
        return argv

    def session_setup(
        self, task: AgentTask, session: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        if task.permissions != "bypass":
            mode = "accept-edits" if task.mode == "workspace_write" else "default"
            calls.append(("session/set_mode", {"modeId": mode}))
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
                    {"configId": "thought_level", "value": level},
                )
            )
        return calls
