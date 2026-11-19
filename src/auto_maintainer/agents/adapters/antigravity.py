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
edits and commands), ``accept-edits``, ``plan``, and since 1.5.11 ``auto`` (approves
edits and safe commands itself, asks before irreversible actions). A task in auto
mode runs in ``auto`` where the CLI offers it, so only what agy itself asks about
reaches the automatic rules and reviewers; elsewhere, and for a restricted task,
tasks that may write run in ``accept-edits`` and read-only tasks in ``default``.
Read-only tasks also get ``--sandbox``, which restricts terminal commands; the option
``sandbox`` gives it to every task or to none instead (on hosts where agy's sandbox
server cannot start, every command run in it fails). The option ``tool_policy`` sets
agy's tool execution policy for each session (``--settings``, never saved), such as
``always-proceed``, under which terminal commands run without asking; unset, agy
keeps its own, ``request-review``.
The model and the reasoning level are session config options (``model``,
``thought_level``). ``--dangerously-skip-permissions`` is bypass mode.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...domain.agents import (
    AgentChoices,
    AgentQuota,
    AgentTask,
    Capabilities,
    QuotaBucket,
)
from ..acp import AcpRuntime
from ..exec import QUOTA_READ_SECONDS, RunFiles

_LEVELS = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class AntigravityRuntime(AcpRuntime):
    """Antigravity's ACP server, with the options ``sandbox`` and ``tool_policy``
    (``agent_options.ANTIGRAVITY``)."""

    known_choices = AgentChoices(efforts=["low", "medium", "high"])

    max_prompt_bytes = 150_000
    """Antigravity 1.5 cuts a prompt past about 200 KB without saying so, losing its
    end; seen on a review conversation whose diff was large."""

    home_paths = (".gemini",)
    session_paths = (".gemini/antigravity-cli/brain/*",)
    """One folder per conversation, holding its artifacts."""
    login_args = ("acp", "--login")
    login_marker = ".gemini/antigravity-cli/antigravity-oauth-token"
    """Antigravity's sign-in with Google (checked against 1.5.3): without a browser
    it prints a page, then reads the code (or the page's address) it ends on from
    standard input. Over ACP it refuses to sign in without a browser."""

    async def signed_in(self, home: Path | None = None) -> bool | None:
        """Not without ``~/.gemini/antigravity-cli/antigravity-oauth-token`` (checked
        against 1.5.3: without a sign-in, ``agy -p`` waits for one); with it, whether
        ``agy -p /usage`` answers, which spends nothing."""
        root = home or self.account_home or Path.home()
        if not (root / ".gemini/antigravity-cli/antigravity-oauth-token").is_file():
            return False
        out = await self.cli_output(
            "-p", "/usage", "--output-format", "json", home=home, check=False
        )
        if out is None:
            return None
        if "authentication required" in out:
            return False
        try:
            return json.loads(out).get("status") == "SUCCESS"
        except ValueError:
            return None

    default_name = "antigravity"
    default_binary = "agy"
    default_env_allow = ("GEMINI_*", "GOOGLE_*", "ANTIGRAVITY_*", "AGY_*")

    def capabilities(self) -> Capabilities:
        return super().capabilities().model_copy(update={"skill_dirs": True})

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        argv = [binary, "acp", "--disable-telemetry"]
        if task.permissions == "bypass":
            argv.append("--dangerously-skip-permissions")
        else:
            sandbox = self.options.get("sandbox") or "read_only"
            if sandbox == "always" or (
                sandbox == "read_only" and task.mode == "read_only"
            ):
                argv.append("--sandbox")
            if self.options.get("tool_policy"):
                argv += [
                    "--settings",
                    json.dumps({"autoExecutionPolicy": self.options["tool_policy"]}),
                ]
        for directory in [*task.skill_dirs, *task.extra_dirs, *task.reference_dirs]:
            argv += ["--add-dir", str(directory)]
        return argv

    def session_setup(
        self, task: AgentTask, session: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        if task.permissions != "bypass":
            offered = {
                str(m.get("id"))
                for m in ((session.get("modes") or {}).get("availableModes") or [])
                if isinstance(m, dict)
            }
            if task.permissions == "auto" and "auto" in offered:
                mode = "auto"
            else:
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

    async def quota(self) -> AgentQuota | None:
        """``agy -p /usage --output-format json`` (checked against 1.5.3): the slash
        command runs no turn and spends no tokens."""
        out = await self.cli_output(
            "-p", "/usage", "--output-format", "json", timeout=QUOTA_READ_SECONDS
        )
        now = datetime.now(UTC)
        if out is None:
            return AgentQuota(
                runtime=self.name, read_at=now, note="agy /usage did not answer"
            )
        try:
            data = json.loads(out)
        except ValueError:
            return AgentQuota(
                runtime=self.name, read_at=now, note="agy /usage said no JSON"
            )
        return agy_quota(self.name, data, now)


def agy_quota(name: str, data: Any, now: datetime) -> AgentQuota:
    """The limits in ``agy /usage``'s JSON answer: ``command.data.groups``, each with
    ``buckets`` that carry ``remaining_fraction`` and ``reset_time``."""
    command = data.get("command") if isinstance(data, dict) else None
    info = (command or {}).get("data") or {}
    buckets = []
    for group in info.get("groups") or []:
        for b in group.get("buckets") or []:
            remaining = b.get("remaining_fraction")
            buckets.append(
                QuotaBucket(
                    group=str(group.get("name") or ""),
                    name=str(b.get("name") or b.get("id") or ""),
                    remaining=float(remaining)
                    if isinstance(remaining, int | float)
                    else None,
                    resets_at=_time(b.get("reset_time")),
                )
            )
    return AgentQuota(
        runtime=name,
        read_at=now,
        buckets=buckets,
        note=str(info.get("description") or "")
        if buckets
        else "agy reported no limits",
    )


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
