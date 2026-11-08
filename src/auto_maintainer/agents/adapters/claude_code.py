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

"""Claude Code (``claude``), checked against version 2.1.318.

Print mode with ``--output-format stream-json --verbose`` emits one JSON object per
line: ``system`` events (``init`` carries the session), ``assistant`` and ``user``
messages, and a closing ``result`` event with ``result``, ``structured_output``
(when ``--json-schema`` was given), ``session_id``, ``usage``, ``total_cost_usd``,
and ``is_error``.

The run is a conversation (``--input-format stream-json``): the service sends an
``initialize`` control request and the prompt as a user message, and keeps standard
input open. In ``auto`` mode (``--permission-mode auto``) Claude Code's classifier
approves safe actions on its own; with ``--permission-prompt-tool stdio`` everything
it would ask a person arrives as a ``control_request`` of subtype ``can_use_tool``
(tool name, input, description, blocked path), which a reviewer answers on the
dashboard with ``allow`` or ``deny``. Standard input closes after the ``result``.

A task that may not run code on the host (``AgentTask.run_code`` below ``any``) also
gets ``--disallowedTools`` for the common code runners (``CODE_RUNNERS``).

``restricted`` runs keep ``--permission-prompts none``: read-only tasks use
``--permission-mode dontAsk``, writing tasks ``acceptEdits``, each with an
``--allowedTools`` list, and anything else is denied without a prompt. Read-only
tasks get a ``--tools`` set without edit tools in every mode. ``--setting-sources``
is empty by default, so neither a target repository's ``.claude/settings.json`` nor
the host user's settings (whose allow rules would skip the prompts) apply, and
``--strict-mcp-config`` keeps a repository's ``.mcp.json`` servers from starting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from ...domain.agents import (
    AgentAnswer,
    AgentChoices,
    AgentEvent,
    AgentEventType,
    AgentQuestion,
    AgentTask,
    Capabilities,
    ModelChoice,
    QuestionKind,
    QuestionOption,
)
from ..exec import Ask, Conversation, ExecRuntime, Invocation, RunFiles, Step
from . import loads_object, partial, short

CODE_RUNNERS = (
    "python", "python3", "pytest", "uv", "uvx", "pip", "pip3", "bash", "sh", "zsh",
    "make", "npm", "npx", "node", "torchrun", "ray", "docker", "cargo", "go",
    "curl", "wget",
)  # fmt: skip
"""Commands that run code or fetch it; a task that may not run code on the host gets
each as ``--disallowedTools Bash(<command> *)``, since Claude Code's own auto mode
would otherwise approve some of them without asking."""

READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "Bash")
READ_ONLY_ALLOWED = (
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
    "Bash(git blame:*)",
    "Bash(git grep:*)",
    "Bash(git ls-files:*)",
    "Bash(ls:*)",
)
WRITE_ALLOWED = (
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "Bash(git:*)",
    "Bash(uv run:*)",
    "Bash(uv sync:*)",
    "Bash(pytest:*)",
    "Bash(ruff:*)",
    "Bash(ls:*)",
)


class ClaudeCodeRuntime(ExecRuntime):
    """Claude Code in print mode.

    Args:
        setting_sources: Value for ``--setting-sources``; empty by default so neither
            target repositories nor the host user's settings change permissions.
        read_only_allowed: ``--allowedTools`` entries for read-only tasks.
        write_allowed: ``--allowedTools`` entries for ``workspace_write`` tasks.
        **kwargs: Passed to ``ExecRuntime``.
    """

    known_choices = AgentChoices(
        models=[
            ModelChoice(id="opus", name="Opus (latest)"),
            ModelChoice(id="sonnet", name="Sonnet (latest)"),
            ModelChoice(id="haiku", name="Haiku (latest)"),
        ],
        efforts=["low", "medium", "high", "xhigh", "max"],
    )

    default_name = "claude-code"
    default_binary = "claude"
    default_env_allow = (
        "ANTHROPIC_*",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_*",
        "AWS_*",
        "GOOGLE_*",
        "VERTEX_*",
    )

    def __init__(
        self,
        name: str | None = None,
        *,
        setting_sources: str = "",
        read_only_allowed: Iterable[str] = READ_ONLY_ALLOWED,
        write_allowed: Iterable[str] = WRITE_ALLOWED,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.setting_sources = setting_sources
        self.read_only_allowed = list(read_only_allowed)
        self.write_allowed = list(write_allowed)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            schema_flag=True,
            read_only_mode=True,
            sessions=True,
            acp=False,
            streaming=True,
            skill_dirs=True,
            bypass=True,
            questions=True,
        )

    def bypass_env(self) -> dict[str, str]:
        """Claude Code refuses ``--dangerously-skip-permissions`` as root unless told it
        runs in a sandbox; operators accept that by setting ``allow_bypass``."""
        return {"IS_SANDBOX": "1"} if os.geteuid() == 0 else {}

    def provider_env(self) -> dict[str, str | None]:
        """Point Claude Code at an Anthropic-compatible endpoint.

        The key goes in ``ANTHROPIC_AUTH_TOKEN`` (sent as a bearer token); an inherited
        ``ANTHROPIC_API_KEY`` would take precedence over it, so it is removed. The
        background model is the same one, since the endpoint may not serve Claude's.
        """
        p = self.provider
        if p is None:
            return {}
        env: dict[str, str | None] = {
            "ANTHROPIC_BASE_URL": p.base_url,
            "ANTHROPIC_AUTH_TOKEN": p.api_key or None,
            "ANTHROPIC_API_KEY": None,
        }
        if self.model:
            env["ANTHROPIC_MODEL"] = self.model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = self.model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = self.model
        return env

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        argv = [
            binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if task.permissions == "bypass":
            argv += ["--dangerously-skip-permissions", "--permission-prompts", "none"]
        elif task.permissions == "auto":
            argv += ["--permission-mode", "auto", "--permission-prompt-tool", "stdio"]
        else:
            argv += [
                "--permission-prompts",
                "none",
                "--permission-mode",
                "dontAsk" if task.mode == "read_only" else "acceptEdits",
            ]
        if task.permissions != "bypass":
            if task.mode == "read_only":
                argv += ["--tools", ",".join(READ_ONLY_TOOLS)]
                argv += ["--allowedTools", *self.read_only_allowed]
            else:
                argv += ["--allowedTools", *self.write_allowed]
            if task.run_code != "any":
                argv += ["--disallowedTools", *(f"Bash({c} *)" for c in CODE_RUNNERS)]
        argv += ["--setting-sources", self.setting_sources]
        argv.append("--strict-mcp-config")
        if task.output_schema is not None:
            argv += [
                "--json-schema",
                json.dumps(task.output_schema, separators=(",", ":")),
            ]
        dirs = [str(d) for d in [*task.skill_dirs, *task.extra_dirs]]
        if dirs:
            argv += ["--add-dir", *dirs]
        if task.resume_session:
            argv += ["--resume", task.resume_session]
        model = task.model or self.model
        if model:
            argv += ["--model", model]
        if task.effort:
            argv += ["--effort", task.effort]
        argv += self.extra_args
        return Invocation(argv=argv)

    def conversation(self, task: AgentTask, files: RunFiles) -> Conversation:
        return ClaudeConversation(self, task)

    def parse_line(self, line: str) -> list[AgentEvent]:
        data = loads_object(line)
        if data is None:
            return super().parse_line(line)
        kind = data.get("type")
        if kind == "system":
            return (
                [partial(session=data.get("session_id"))]
                if data.get("subtype") == "init"
                else []
            )
        if kind == "assistant":
            events: list[AgentEvent] = []
            for block in (data.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    events.append(
                        AgentEvent(type=AgentEventType.MESSAGE, text=block["text"])
                    )
                elif btype == "thinking" and block.get("thinking"):
                    events.append(
                        AgentEvent(
                            type=AgentEventType.THINKING, text=str(block["thinking"])
                        )
                    )
                elif btype == "tool_use":
                    events.append(
                        AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            text=str(block.get("name", "")),
                            data={
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": block.get("input"),
                            },
                        )
                    )
            return events
        if kind == "user":
            events = []
            content = (data.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    events.append(
                        AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            text=short(block.get("content", ""), 500),
                            data={
                                "tool_use_id": block.get("tool_use_id"),
                                "is_error": bool(block.get("is_error")),
                            },
                        )
                    )
            return events
        if kind == "result":
            usage = data.get("usage") or {}
            is_error = bool(data.get("is_error")) or data.get("subtype") != "success"
            error = None
            if is_error:
                error = str(
                    data.get("result")
                    or data.get("subtype")
                    or "claude reported an error"
                )
                if data.get("api_error_status"):
                    error = f"{error} (API status {data['api_error_status']})"
            return [
                partial(
                    text=data.get("result")
                    if isinstance(data.get("result"), str)
                    else None,
                    structured=data.get("structured_output")
                    if isinstance(data.get("structured_output"), dict)
                    else None,
                    session=data.get("session_id"),
                    usage={
                        "input_tokens": int(usage.get("input_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                        "cost_usd": data.get("total_cost_usd"),
                    },
                    is_error=is_error or None,
                    error=error,
                )
            ]
        return []


class ClaudeConversation(Conversation):
    """Claude Code's stream-json control protocol for one task."""

    def __init__(self, runtime: ClaudeCodeRuntime, task: AgentTask) -> None:
        self.runtime = runtime
        self.task = task

    def opening(self) -> list[str]:
        return [
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": f"init-{self.task.id}",
                    "request": {"subtype": "initialize", "hooks": None},
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": self.task.prompt},
                },
                ensure_ascii=False,
            ),
        ]

    def feed(self, line: str) -> Step:
        data = loads_object(line)
        if data is not None and data.get("type") == "control_request":
            return self._control(data)
        if data is not None and data.get("type") in (
            "control_response",
            "control_cancel_request",
        ):
            return Step()
        return Step(
            events=self.runtime.parse_line(line),
            done=data is not None and data.get("type") == "result",
        )

    def answer(self, ask: Ask, answer: AgentAnswer) -> list[str]:
        request_id, tool_input = ask.token
        if answer.allow:
            response: dict[str, Any] = {"behavior": "allow", "updatedInput": tool_input}
        else:
            response = {
                "behavior": "deny",
                "message": answer.message or "A reviewer denied this request.",
            }
        return [
            json.dumps(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": response,
                    },
                },
                ensure_ascii=False,
            )
        ]

    def _control(self, data: dict[str, Any]) -> Step:
        request = data.get("request") or {}
        request_id = data.get("request_id")
        if request.get("subtype") != "can_use_tool":
            return Step(
                send=[
                    json.dumps(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "error",
                                "request_id": request_id,
                                "error": f"{request.get('subtype')} is not supported",
                            },
                        }
                    )
                ]
            )
        tool = str(request.get("tool_name") or "")
        tool_input = request.get("input") or {}
        detail = (
            str(tool_input.get("command"))
            if isinstance(tool_input, dict) and tool_input.get("command")
            else short(tool_input, 4000)
        )
        description = str(request.get("description") or "")
        paths = [str(request["blocked_path"])] if request.get("blocked_path") else []
        if isinstance(tool_input, dict) and tool_input.get("file_path"):
            paths.append(str(tool_input["file_path"]))
        question = AgentQuestion(
            kind=QuestionKind.PERMISSION,
            title=f"use {tool}"
            + (f": {short(description, 200)}" if description else ""),
            detail=detail,
            tool=tool,
            paths=paths,
            options=[
                QuestionOption(id="allow", label="Allow", kind="allow"),
                QuestionOption(id="deny", label="Deny", kind="deny"),
            ],
        )
        return Step(asks=[Ask(question=question, token=(request_id, tool_input))])
