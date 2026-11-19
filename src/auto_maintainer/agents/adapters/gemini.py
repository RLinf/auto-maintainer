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

A model provider that speaks Google's Gemini API reaches it through
``GOOGLE_GEMINI_BASE_URL`` (the address without its ``/v1beta``; Gemini CLI wants
HTTPS there unless it is the local host) with the key in ``GEMINI_API_KEY``, which
``docs/reference/configuration.md`` documents for API-key sign-in; an inherited
``GOOGLE_API_KEY``, which would win over it, is removed. Not run end to end.

The options that are set (``agent_options.GEMINI``) go into a settings file in the
run's own folder, which ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` names: the highest of
Gemini CLI's settings files, for that run alone; ``~/.gemini/settings.json`` is never
written.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ...domain.agents import AgentTask, Capabilities
from ..acp import AcpRuntime
from ..exec import RunFiles, write_private

GEMINI_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
"""The keys Gemini CLI reads from its environment."""
GEMINI_SIGN_INS = ("oauth_creds.json", "gemini-credentials.json")
"""The files in ``~/.gemini`` that hold its sign-in with Google."""
SETTINGS_FILE = "gemini-settings.json"
"""The run's own settings file, in its folder."""
_SETTINGS: tuple[tuple[str, str, Any], ...] = (
    ("tool_sandboxing", "security.toolSandboxing", bool),
    ("sandbox_network", "tools.sandboxNetworkAccess", bool),
    ("checkpointing", "general.checkpointing.enabled", bool),
    ("max_session_turns", "model.maxSessionTurns", int),
    ("compression_threshold", "model.compressionThreshold", float),
    ("respect_gitignore", "context.fileFiltering.respectGitIgnore", bool),
    ("shell_inactivity_timeout", "tools.shell.inactivityTimeout", int),
    ("loop_detection", "model.disableLoopDetection", lambda v: not v),
    ("usage_statistics", "privacy.usageStatisticsEnabled", bool),
    ("auto_update", "general.enableAutoUpdate", bool),
)
"""Each option, the settings key it sets, and how its value is written."""


class GeminiRuntime(AcpRuntime):
    """Gemini CLI's ACP server."""

    home_paths = (".gemini",)
    session_paths = (".gemini/tmp/*/chats/*",)
    default_name = "gemini"
    default_binary = "gemini"
    default_env_allow = ("GEMINI_*", "GOOGLE_*")

    def __init__(self, name: str | None = None, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self._settings_files: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return super().capabilities().model_copy(update={"skill_dirs": True})

    def cli_settings(self) -> dict[str, Any]:
        """The options set, as Gemini CLI's nested settings."""
        settings: dict[str, Any] = {}
        for key, path, write in _SETTINGS:
            if self.options.get(key) is None:
                continue
            *parents, last = path.split(".")
            here = settings
            for part in parents:
                here = here.setdefault(part, {})
            here[last] = write(self.options[key])
        return settings

    def task_env(self, task: AgentTask) -> dict[str, str]:
        path = self._settings_files.pop(task.id, None)
        return {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": path} if path else {}

    async def signed_in(self, home: Path | None = None) -> bool | None:
        """Whether Gemini CLI has an account to run on (checked against 0.68.0): a
        key in the service's environment, or its sign-in with Google in ``~/.gemini``
        (``oauth_creds.json``, or the encrypted ``gemini-credentials.json``). Without
        any, every task ends at once with "Gemini API key is missing or not
        configured". ``None`` when its ``settings.json`` chose a sign-in that leaves
        no such file (Vertex AI, a keychain)."""
        if any(os.environ.get(k) for k in GEMINI_KEYS):
            return True
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
            return None
        root = (home or self.account_home or Path.home()) / ".gemini"
        if any((root / name).is_file() for name in GEMINI_SIGN_INS):
            return True
        try:
            settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return None
        auth = (settings.get("security") or {}).get("auth") or {}
        chosen = auth.get("selectedType") or settings.get("selectedAuthType")
        return None if chosen and chosen != "gemini-api-key" else False

    def provider_env(self) -> dict[str, str | None]:
        p = self.provider
        if p is None or p.protocol != "google":
            return {}
        return {
            "GOOGLE_GEMINI_BASE_URL": re.sub(r"/v1(beta)?/?$", "", p.base_url),
            "GEMINI_API_KEY": p.api_key or None,
            "GOOGLE_API_KEY": None,
        }

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        if task.permissions == "bypass":
            mode = "yolo"
        else:
            mode = "auto_edit" if task.mode == "workspace_write" else "default"
        argv = [binary, "--experimental-acp", "--approval-mode", mode]
        settings = self.cli_settings()
        if settings:
            path = files.dir / SETTINGS_FILE
            write_private(path, json.dumps(settings))
            self._settings_files[task.id] = str(path)
        model = task.model or self.model
        if model:
            argv += ["-m", model]
        dirs = [
            str(d) for d in [*task.skill_dirs, *task.extra_dirs, *task.reference_dirs]
        ]
        if dirs:
            argv += ["--include-directories", ",".join(dirs)]
        return argv
