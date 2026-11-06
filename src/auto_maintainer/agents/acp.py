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

"""The Agent Client Protocol (ACP) transport: JSON-RPC 2.0 over standard input and output.

The service is the client. It sends ``initialize``, ``session/new`` with the task's
working directory, the adapter's setup calls (mode, model), and ``session/prompt``
with the prompt. ``session/update`` notifications carry the agent's messages and tool
calls; the ``session/prompt`` response carries ``stopReason`` and, for some agents,
``usage``. The agent's ``session/request_permission`` requests become questions: the
automatic rules in ``approver`` settle the clearly safe ones and a reviewer the rest.
The client declares no file-system or terminal capabilities, so the agent uses its
own tools. Checked against Antigravity 1.5.1 (``agy acp``); OpenCode 2.0.32 passed
the handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from ..domain.agents import (
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
from .adapters import loads_object, partial, short
from .approver import auto_answer
from .exec import (
    Ask,
    Conversation,
    ExecRuntime,
    Invocation,
    RunFiles,
    Step,
    classify_failure,
    isolation_env,
    resolve_binary,
)
from .sandbox import build_env

_OPTION_KINDS = {
    "allow_once": "allow",
    "allow_always": "allow_session",
    "reject_once": "deny",
    "reject_always": "deny",
}
_OK_STOPS = {"end_turn", "max_tokens", "max_turn_requests"}


class AcpRuntime(ExecRuntime):
    """Base class for CLIs that speak ACP; subclasses build the command line."""

    def capabilities(self) -> Capabilities:
        return Capabilities(
            schema_flag=False,
            read_only_mode=False,
            sessions=False,
            acp=True,
            streaming=True,
            skill_dirs=False,
            bypass=True,
            questions=True,
        )

    def acp_argv(self, task: AgentTask, files: RunFiles, binary: str) -> list[str]:
        """The command that starts the ACP server; subclasses override."""
        raise NotImplementedError

    async def choices(self) -> AgentChoices | None:
        """Ask the CLI itself: start its ACP server, open a session, read the model
        and reasoning options it offers, and stop. No prompt is sent, so it costs no
        tokens. Kept for ``CHOICES_TTL`` seconds."""
        cached = getattr(self, "_choices", None)
        if cached is not None and time.monotonic() - cached[0] < CHOICES_TTL:
            return cached[1]
        binary = resolve_binary(self.binary, self.search_dirs)
        if binary is None:
            return None
        with tempfile.TemporaryDirectory(prefix="am-acp-probe-") as tmp:
            root = Path(tmp)
            task = AgentTask(
                kind="probe", prompt="", workdir=root, mode="workspace_write"
            )
            files = RunFiles.create(root, "probe")
            argv = [*self.acp_argv(task, files, binary), *self.extra_args]
            if self.command_prefix:
                fill = {"workdir": str(root), "run_dir": str(files.dir)}
                argv = [*(p.format(**fill) for p in self.command_prefix), *argv]
            env = build_env(task, self.env_allow)
            env.update(isolation_env(files.dir))
            env.update(self.task_env(task))
            for name, value in self.provider_env().items():
                if value is None:
                    env.pop(name, None)
                else:
                    env[name] = value
            session = await _open_session(argv, env, root)
        found = choices_from_session(session) if session is not None else None
        # A CLI that did not answer (still starting, not signed in) is asked again
        # within a minute instead of an hour.
        kept = time.monotonic() - (0 if found else CHOICES_TTL - CHOICES_RETRY)
        self._choices = (kept, found)
        return found

    def session_setup(
        self, task: AgentTask, session: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Calls to make after ``session/new`` and before the prompt (mode, model)."""
        return []

    def prompt_text(self, task: AgentTask) -> str:
        return task.prompt

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        return Invocation(argv=[*self.acp_argv(task, files, binary), *self.extra_args])

    def conversation(self, task: AgentTask, files: RunFiles) -> Conversation:
        return AcpConversation(self, task)

    def auto_answer(
        self, task: AgentTask, question: AgentQuestion
    ) -> AgentAnswer | None:
        if task.permissions == "bypass":
            return AgentAnswer(allow=True, by="service", automatic=True)
        return auto_answer(task, question)


CHOICES_TTL = 3600.0
"""Seconds a CLI's reported models and reasoning levels are reused."""
CHOICES_RETRY = 60.0
"""Seconds before asking again a CLI that did not report them."""


def choices_from_session(session: dict[str, Any]) -> AgentChoices:
    """The models and reasoning levels in a ``session/new`` result: its config
    options of category ``model`` and ``thought_level``, else its ``models``."""
    models: list[ModelChoice] = []
    default_model: str | None = None
    efforts: list[str] = []
    default_effort: str | None = None
    for option in session.get("configOptions") or []:
        if not isinstance(option, dict):
            continue
        values = [
            ModelChoice(
                id=str(o.get("value") or o.get("id")), name=str(o.get("name") or "")
            )
            for o in option.get("options") or []
            if isinstance(o, dict) and (o.get("value") or o.get("id"))
        ]
        kind = option.get("category") or option.get("id")
        current = option.get("currentValue")
        if kind == "model":
            models, default_model = values, str(current) if current else None
        elif kind in ("thought_level", "effort", "reasoning_effort"):
            efforts = [v.id for v in values]
            default_effort = str(current) if current else None
    if not models and isinstance(session.get("models"), dict):
        listed = session["models"]
        models = [
            ModelChoice(id=str(m["modelId"]), name=str(m.get("name") or ""))
            for m in listed.get("availableModels") or []
            if isinstance(m, dict) and m.get("modelId")
        ]
        current = listed.get("currentModelId")
        default_model = str(current) if current else None
    return AgentChoices(
        models=models,
        default_model=default_model,
        efforts=efforts,
        default_effort=default_effort,
        source="cli",
    )


async def _open_session(
    argv: list[str], env: dict[str, str], cwd: Path, timeout: float = 30.0
) -> dict[str, Any] | None:
    """Start an ACP server, send ``initialize`` and ``session/new``, and return the
    session's result; ``None`` when it does not answer in ``timeout`` seconds."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def call(request_id: int, method: str, params: dict[str, Any]) -> Any:
        assert proc.stdin is not None and proc.stdout is not None
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None
            reply = loads_object(line.decode("utf-8", "replace"))
            if reply is not None and reply.get("id") == request_id:
                return reply.get("result")

    async def handshake() -> dict[str, Any] | None:
        await call(
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        result = await call(2, "session/new", {"cwd": str(cwd), "mcpServers": []})
        return result if isinstance(result, dict) else None

    try:
        return await asyncio.wait_for(handshake(), timeout)
    except (TimeoutError, OSError, ValueError):
        return None
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)


class AcpConversation(Conversation):
    """One ACP session for one task."""

    def __init__(self, runtime: AcpRuntime, task: AgentTask) -> None:
        self.runtime = runtime
        self.task = task
        self.next_id = 0
        self.pending: dict[str, str] = {}
        self.setup: list[tuple[str, dict[str, Any]]] = []
        self.session_id: str | None = None
        self.current: list[str] = []
        self.last_message = ""

    # -- Conversation ------------------------------------------------------------------

    def opening(self) -> list[str]:
        return [
            self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
            )
        ]

    def feed(self, line: str) -> Step:
        msg = loads_object(line)
        if msg is None:
            return Step()
        if "method" in msg and "id" in msg:
            return self._agent_request(msg)
        if "method" in msg:
            if msg["method"] == "session/update":
                return Step(
                    events=self._update((msg.get("params") or {}).get("update") or {})
                )
            return Step()
        if "id" in msg:
            return self._response(msg)
        return Step()

    def answer(self, ask: Ask, answer: AgentAnswer) -> list[str]:
        request_id, options = ask.token
        wanted = {"allow", "allow_session"} if answer.allow else {"deny"}
        chosen = (
            answer.option_id
            if any(o["optionId"] == answer.option_id for o in options)
            else None
        )
        if chosen is None:
            order = ["allow"] if answer.allow else ["deny"]
            for kind in order:
                chosen = next(
                    (
                        o["optionId"]
                        for o in options
                        if _OPTION_KINDS.get(o.get("kind")) == kind
                    ),
                    None,
                )
                if chosen:
                    break
        if chosen is None or _OPTION_KINDS.get(_kind_of(options, chosen)) not in wanted:
            outcome: dict[str, Any] = {"outcome": "cancelled"}
        else:
            outcome = {"outcome": "selected", "optionId": chosen}
        return [
            _line({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})
        ]

    # -- internals ---------------------------------------------------------------------

    def _request(self, method: str, params: dict[str, Any]) -> str:
        self.next_id += 1
        self.pending[str(self.next_id)] = method
        return _line(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        )

    def _response(self, msg: dict[str, Any]) -> Step:
        method = self.pending.pop(str(msg.get("id")), None)
        if method is None:
            return Step()
        if "error" in msg:
            error = msg.get("error") or {}
            text = str(error.get("message") or error)
            if method in ("initialize", "session/new", "session/prompt"):
                kind = classify_failure(None, "", text)
                return Step(
                    events=[
                        AgentEvent(type=AgentEventType.ERROR, text=text),
                        partial(
                            is_error=True,
                            error=f"{method} failed: {text}",
                            error_kind=kind,
                        ),
                    ],
                    done=True,
                )
            return Step(
                events=[
                    AgentEvent(
                        type=AgentEventType.MESSAGE, text=f"[{method} failed: {text}]"
                    )
                ],
                send=self._next_setup(),
            )
        result = msg.get("result") or {}
        if method == "initialize":
            params: dict[str, Any] = {"cwd": str(self.task.workdir), "mcpServers": []}
            return Step(send=[self._request("session/new", params)])
        if method == "session/new":
            self.session_id = str(result.get("sessionId") or "")
            self.setup = list(self.runtime.session_setup(self.task, result))
            return Step(
                events=[partial(session=self.session_id)], send=self._next_setup()
            )
        if method == "session/prompt":
            self._flush()
            stop = str(result.get("stopReason") or "")
            usage = result.get("usage") or {}
            error = (
                None if stop in _OK_STOPS else f"the agent stopped: {stop or 'unknown'}"
            )
            return Step(
                events=[
                    partial(
                        text=self.last_message,
                        usage={
                            "input_tokens": int(usage.get("inputTokens") or 0),
                            "output_tokens": int(usage.get("outputTokens") or 0)
                            + int(usage.get("thoughtTokens") or 0),
                        }
                        if usage
                        else None,
                        is_error=bool(error) or None,
                        error=error,
                    )
                ],
                done=True,
            )
        return Step(send=self._next_setup())

    def _next_setup(self) -> list[str]:
        if self.setup:
            method, params = self.setup.pop(0)
            return [self._request(method, {"sessionId": self.session_id, **params})]
        if (
            self.session_id is not None
            and "session/prompt" not in self.pending.values()
        ):
            return [
                self._request(
                    "session/prompt",
                    {
                        "sessionId": self.session_id,
                        "prompt": [
                            {
                                "type": "text",
                                "text": self.runtime.prompt_text(self.task),
                            }
                        ],
                    },
                )
            ]
        return []

    def _agent_request(self, msg: dict[str, Any]) -> Step:
        method = msg.get("method")
        if method != "session/request_permission":
            return Step(
                send=[
                    _line(
                        {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "error": {
                                "code": -32601,
                                "message": f"{method} is not supported",
                            },
                        }
                    )
                ]
            )
        params = msg.get("params") or {}
        call = params.get("toolCall") or {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        raw = call.get("rawInput") or call.get("args") or {}
        command = ""
        if isinstance(raw, dict):
            command = str(
                raw.get("command") or raw.get("CommandLine") or raw.get("cmd") or ""
            )
        paths = [
            str(loc.get("path"))
            for loc in call.get("locations") or []
            if isinstance(loc, dict) and loc.get("path")
        ]
        kind = str(call.get("kind") or "other")
        title = str(call.get("title") or call.get("name") or kind)
        question = AgentQuestion(
            kind=QuestionKind.PERMISSION,
            title=f"{_VERBS.get(kind, 'use a tool')}: {short(title, 200)}",
            detail=command or short(raw, 4000),
            tool=kind,
            paths=paths,
            options=[
                QuestionOption(
                    id=str(o.get("optionId")),
                    label=str(o.get("name") or o.get("optionId")),
                    kind=_OPTION_KINDS.get(str(o.get("kind")), "deny"),  # type: ignore[arg-type]
                )
                for o in options
            ],
        )
        if kind == "execute" and not command:
            question = question.model_copy(update={"detail": short(raw, 4000)})
        return Step(asks=[Ask(question=question, token=(msg["id"], options))])

    def _update(self, update: dict[str, Any]) -> list[AgentEvent]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if text:
                self.current.append(str(text))
                return [AgentEvent(type=AgentEventType.MESSAGE, text=str(text))]
            return []
        if kind == "agent_thought_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            return (
                [AgentEvent(type=AgentEventType.THINKING, text=str(text))]
                if text
                else []
            )
        if kind == "tool_call":
            self._flush()
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    text=str(update.get("title") or update.get("kind") or ""),
                    data={"id": update.get("toolCallId"), "kind": update.get("kind")},
                )
            ]
        if kind == "tool_call_update" and update.get("status") in (
            "completed",
            "failed",
        ):
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    text=str(update.get("title") or ""),
                    data={
                        "id": update.get("toolCallId"),
                        "is_error": update.get("status") == "failed",
                    },
                )
            ]
        return []

    def _flush(self) -> None:
        text = "".join(self.current).strip()
        if text:
            self.last_message = text
        self.current = []


_VERBS = {
    "read": "read",
    "edit": "edit",
    "delete": "delete",
    "move": "move",
    "search": "search",
    "execute": "run",
    "fetch": "fetch",
    "think": "think",
}


def _kind_of(options: list[dict[str, Any]], option_id: str) -> str:
    return next(
        (str(o.get("kind")) for o in options if o.get("optionId") == option_id), ""
    )


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)
