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

"""Gemini CLI (``gemini --experimental-acp``), checked against version 0.9.0.

The one-shot mode cannot hand a permission prompt to the caller, so the service runs
Gemini's ACP server (see ``agents.acp``). ``--approval-mode`` decides what asks:
``auto_edit`` approves edits on its own and asks before other tools, so tasks that
may write use it; read-only tasks use ``default``, which asks before every tool that
needs approval; bypass mode is ``yolo``. On this host the ACP server did not answer
because the account needs ``GOOGLE_CLOUD_PROJECT``, so the flow follows the ACP
specification and was not run end to end.
"""

from __future__ import annotations

from ...domain.agents import AgentTask, Capabilities
from ..acp import AcpRuntime
from ..exec import RunFiles


class GeminiRuntime(AcpRuntime):
    """Gemini CLI's ACP server."""

    home_paths = (".gemini",)
    default_name = "gemini"
    default_binary = "gemini"
    default_env_allow = ("GEMINI_*", "GOOGLE_*")

    def capabilities(self) -> Capabilities:
        return super().capabilities().model_copy(update={"skill_dirs": True})

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        if task.permissions == "bypass":
            mode = "yolo"
        else:
            mode = "auto_edit" if task.mode == "workspace_write" else "default"
        argv = [binary, "--experimental-acp", "--approval-mode", mode]
        model = task.model or self.model
        if model:
            argv += ["-m", model]
        dirs = [
            str(d) for d in [*task.skill_dirs, *task.extra_dirs, *task.reference_dirs]
        ]
        if dirs:
            argv += ["--include-directories", ",".join(dirs)]
        return argv
