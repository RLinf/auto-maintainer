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

pi cannot ask: its ``bash`` tool runs anything. Its interactive UI, where it signs in
(``/login``), also runs any shell command typed after ``!``, so the service never
shows it to anyone: the dashboard signs pi in to a subscription (``login_options``)
through ``agents/pi_login.mjs``, which calls the sign-in that UI calls
(``AuthStorage.login`` of pi's own package) and relays only the page to open and the
code to paste, the way every CLI's sign-in is relayed (``agents/login.py``). Outside bypass mode a task therefore
gets only the tools that cannot run code (``--tools``): read, grep, find, and ls, with
edit and write for a task that may change its checkout. In a container the service
runs it in bypass mode with every tool. It signs in with ``/login`` (kept in
``~/.pi/agent``, mounted into containers) or a provider's API key in its usual
environment variable; a model provider from the service settings goes into a
``models.json`` of the run's own (``PI_CODING_AGENT_DIR``), whose ``apiKey`` names
the environment variable ``AM_PROVIDER_API_KEY`` instead of holding the key (pi reads
a value that names a set variable from it, ``core/resolve-config-value``), with what
is known of the model: ``contextWindow``, ``maxTokens``, ``reasoning``, ``input``,
and a ``thinkingLevelMap`` that hides the levels it does not accept
(``docs/models.md``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ...domain.agents import AgentEvent, AgentEventType, AgentTask, Capabilities
from ...domain.settings import ModelSpec
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
    "google": "google-generative-ai",
}
_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
"""pi's thinking levels above ``off``; the service's ``max`` is pi's ``xhigh``."""
PROVIDER = "am"
"""The name of the service's model provider in a run's ``models.json``."""
LOGIN_SCRIPT = Path(__file__).resolve().parent.parent / "pi_login.mjs"
PACKAGE = "@mariozechner/pi-coding-agent"


def package_of(binary: str) -> Path | None:
    """pi's package folder, found from its command (a link to its ``dist/cli.js``)."""
    for folder in Path(binary).resolve().parents:
        manifest = folder / "package.json"
        if manifest.is_file():
            try:
                name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError, AttributeError):
                return None
            return folder if name == PACKAGE else None
    return None


def model_entry(model: str, spec: ModelSpec | None) -> dict[str, Any]:
    """The ``models.json`` entry of ``model``, with what its provider's settings know
    of it; pi's defaults stand for the rest."""
    entry: dict[str, Any] = {"id": model}
    if spec is None:
        return entry
    if spec.context_window:
        entry["contextWindow"] = spec.context_window
    if spec.max_output:
        entry["maxTokens"] = spec.max_output
    if spec.images is not None:
        entry["input"] = ["text", "image"] if spec.images else ["text"]
    if spec.reasoning:
        accepted = {"xhigh" if level == "max" else level for level in spec.reasoning}
        entry["reasoning"] = True
        hidden = {level: None for level in _LEVELS if level not in accepted}
        if hidden:
            entry["thinkingLevelMap"] = hidden
    elif spec.tested_at:
        entry["reasoning"] = False
    return entry


class PiRuntime(ExecRuntime):
    """pi in print mode, one task per run."""

    default_name = "pi"
    default_binary = "pi"
    home_paths = (".pi",)
    login_marker = ".pi/agent/auth.json"
    login_options = (
        ("anthropic", "Claude Pro/Max"),
        ("openai-codex", "ChatGPT Plus/Pro (Codex)"),
        ("github-copilot", "GitHub Copilot"),
    )
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

    def login_command(self, binary: str, option: str | None) -> list[str]:
        package = package_of(binary)
        node = shutil.which("node")
        if package is None or node is None:
            raise RuntimeError("pi's package or Node.js is not where the service looks")
        return [node, str(LOGIN_SCRIPT), str(package), str(option)]

    def login_env(self, env: dict[str, str]) -> dict[str, str]:
        # The sign-in belongs in the account's home, not a folder the host names.
        return {k: v for k, v in env.items() if k != "PI_CODING_AGENT_DIR"}

    async def signed_in(self, home: Path | None = None) -> bool | None:
        """Whether pi holds a sign-in or a key for any provider in the account's
        ``.pi/agent/auth.json``."""
        path = (home or Path.home()) / self.login_marker
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return None
        return isinstance(data, dict) and bool(data)

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
                                "apiKey": "AM_PROVIDER_API_KEY",
                                "models": [model_entry(model, p.spec(model))],
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
