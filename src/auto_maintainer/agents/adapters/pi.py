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

"""pi (``@mariozechner/pi-coding-agent``, the ``pi`` command), checked against the
README, ``src/main.ts``, ``src/modes/print-mode.ts``, ``docs/json.md``, and
``docs/models.md`` of version 0.73.1; not run on this host.

``pi -p --mode json`` reads the prompt piped to standard input (``buildInitialMessage``
puts it first in the opening message) and writes its events as JSON lines: tool calls
start and end, and each message ends with ``message_end``, whose assistant message
holds the text, the usage, and ``stopReason`` (``error`` or ``aborted`` fails the
run, and pi exits 1). ``--no-session`` keeps nothing; ``--offline`` and
``PI_TELEMETRY=0`` stop its update checks and install pings.

pi cannot ask: its ``bash`` tool runs anything. Outside bypass mode a task therefore
gets only the tools that cannot run code (``--tools``): read, grep, find, and ls, with
edit and write for a task that may change its checkout. In a container the service
runs it in bypass mode with every tool. It signs in with ``/login`` (kept in
``~/.pi/agent``, mounted into containers) or a provider's API key in its usual
environment variable; a model provider from the service settings goes into a
``models.json`` of the run's own (``PI_CODING_AGENT_DIR``), whose ``apiKey`` names
``AM_PROVIDER_API_KEY`` instead of holding the key.
"""

from __future__ import annotations

import json
from typing import Any

from ...domain.agents import AgentEvent, AgentEventType, AgentTask, Capabilities
from ..exec import ExecRuntime, Invocation, RunFiles
from . import partial, short

READ_TOOLS = "read,grep,find,ls"
WRITE_TOOLS = "read,grep,find,ls,edit,write"
_THINKING = {
    "off": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
_API = {
    "openai": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic": "anthropic-messages",
}
PROVIDER = "am"
"""The name of the service's model provider in a run's ``models.json``."""


class PiRuntime(ExecRuntime):
    """pi in print mode, one task per run."""

    default_name = "pi"
    default_binary = "pi"
    home_paths = (".pi",)
    default_env_allow = (
        "PI_*",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    )
    line_mode = True

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self._agent_dirs: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, bypass=True, read_only_mode=True)

    def provider_env(self) -> dict[str, str | None]:
        p = self.provider
        return {"AM_PROVIDER_API_KEY": p.api_key} if p is not None and p.api_key else {}

    def task_env(self, task: AgentTask) -> dict[str, str]:
        env = {"PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1"}
        agent_dir = self._agent_dirs.pop(task.id, None)
        if agent_dir:
            env["PI_CODING_AGENT_DIR"] = agent_dir
        return env

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        argv = [binary, "-p", "--mode", "json", "--no-session", "--offline"]
        if task.permissions != "bypass":
            tools = WRITE_TOOLS if task.mode == "workspace_write" else READ_TOOLS
            argv += ["--tools", tools]
        model = task.model or self.model
        p = self.provider
        if p is not None and p.base_url and model and p.protocol in _API:
            agent_dir = files.dir / "pi-agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            PROVIDER: {
                                "baseUrl": p.base_url,
                                "api": _API[p.protocol],
                                "apiKey": "$AM_PROVIDER_API_KEY",
                                "models": [{"id": model}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self._agent_dirs[task.id] = str(agent_dir)
            argv += ["--provider", PROVIDER, "--model", model]
        elif model:
            argv += ["--model", model]
        level = _THINKING.get(task.effort or "")
        if level:
            argv += ["--thinking", level]
        return Invocation(argv=[*argv, *self.extra_args], stdin=task.prompt)

    def parse_line(self, line: str) -> list[AgentEvent]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "tool_execution_start":
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    text=f"{event.get('toolName')}: {short(event.get('args'), 300)}",
                    data={"tool": event.get("toolName"), "args": event.get("args")},
                )
            ]
        if kind == "tool_execution_end":
            result = event.get("result") or {}
            text = "".join(
                c.get("text", "")
                for c in result.get("content") or []
                if isinstance(c, dict)
            )
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    text=short(text, 2000),
                    data={"tool": event.get("toolName"), "error": event.get("isError")},
                )
            ]
        if kind != "message_end":
            return []
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return []
        out: list[AgentEvent] = []
        usage = message.get("usage") or {}
        if usage:
            cost = (usage.get("cost") or {}).get("total")
            out.append(
                AgentEvent(
                    type=AgentEventType.USAGE,
                    data={
                        "input_tokens": int(usage.get("input") or 0)
                        + int(usage.get("cacheRead") or 0),
                        "output_tokens": int(usage.get("output") or 0),
                        "cost_usd": float(cost) if cost is not None else None,
                    },
                )
            )
        if message.get("stopReason") in ("error", "aborted"):
            out.append(
                AgentEvent(
                    type=AgentEventType.ERROR,
                    text=str(message.get("errorMessage") or message.get("stopReason")),
                )
            )
            return out
        content = message.get("content")
        text = (
            content
            if isinstance(content, str)
            else "".join(
                c.get("text", "")
                for c in content or []
                if isinstance(c, dict) and c.get("type") == "text"
            )
        ).strip()
        if text:
            out += [
                AgentEvent(type=AgentEventType.MESSAGE, text=text),
                partial(text=text),
            ]
        return out
