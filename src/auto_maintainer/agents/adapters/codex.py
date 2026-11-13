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

"""Codex (``codex app-server``), checked against version 0.162.1.

``codex exec`` cannot hand a question to its caller, so the service drives the app
server: JSON-RPC over standard input and output. It sends ``initialize``, the
``initialized`` notification, ``thread/start`` (or ``thread/resume``) with the
working directory, sandbox, and approval settings, then ``turn/start`` with the
prompt and the strict output schema. ``item/completed`` notifications carry agent
messages and command results, ``thread/tokenUsage/updated`` the running token
totals, and ``turn/completed`` the end of the turn with its status.

Auto mode is Codex's own: the sandbox (``read-only`` or ``workspace-write``) runs what
it allows, ``approvalPolicy: on-request`` lets the agent ask for more, and
``approvalsReviewer: auto_review`` has Codex's reviewer subagent decide those
escalations. What still reaches the client becomes a question for a reviewer on the
dashboard: ``item/tool/requestUserInput`` (the agent asks the user something),
``item/commandExecution/requestApproval``, ``item/fileChange/requestApproval``, and
``item/permissions/requestApproval``. Operators can set the runtime option
``approvals_reviewer: user`` to send every escalation to reviewers instead; on hosts
where Codex's sandbox cannot start, that is every command. MCP elicitations are
declined. ``restricted`` runs use ``approvalPolicy: never``; bypass mode uses the
``danger-full-access`` sandbox.

``--add-dir`` would make a directory writable, so skill directories are listed in the
prompt instead; the read-only sandbox can read them anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.agents import (
    AgentAnswer,
    AgentChoices,
    AgentEvent,
    AgentEventType,
    AgentTask,
    Capabilities,
    InputQuestion,
    QuestionKind,
    QuestionOption,
)
from ...domain.agents import AgentQuestion as Question
from ..exec import (
    Ask,
    Conversation,
    ExecRuntime,
    Invocation,
    RunFiles,
    Step,
    classify_failure,
)
from ..structured import strict_schema
from . import loads_object, partial, short

_EFFORTS = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
    "minimal": "minimal",
}
_DECISIONS = {
    "accept": ("Allow", "allow"),
    "acceptForSession": ("Allow for this run", "allow_session"),
    "decline": ("Deny", "deny"),
    "cancel": ("Deny and stop the turn", "cancel"),
}


class CodexRuntime(ExecRuntime):
    """Codex's app server, one thread per task.

    Args:
        approvals_reviewer: Who decides escalations in auto mode: ``auto_review``
            (Codex's reviewer subagent, its auto mode) or ``user`` (the dashboard).
        **kwargs: Passed to ``ExecRuntime``.
    """

    known_choices = AgentChoices(efforts=["minimal", "low", "medium", "high", "xhigh"])

    home_paths = (".codex",)
    session_paths = (".codex/sessions/*/*/*",)
    """A day's sessions are in ``sessions/<year>/<month>/<day>``."""
    login_args = ("login", "--device-auth")
    login_marker = ".codex/auth.json"
    """Codex's device login (checked against 0.162.1): it prints a page and a
    one-time code to enter there, and waits."""

    async def signed_in(self, home: Path | None = None) -> bool | None:
        """``codex login status``: ``Logged in using ...`` or ``Not logged in``."""
        out = await self.cli_output(
            "login", "status", home=home, check=False, stderr=True
        )
        if out is None:
            return None
        if "Not logged in" in out:
            return False
        return True if "Logged in" in out else None

    default_name = "codex"
    default_binary = "codex"
    default_env_allow = ("OPENAI_*", "CODEX_HOME", "AZURE_OPENAI_*")

    def __init__(
        self,
        name: str | None = None,
        *,
        approvals_reviewer: str = "auto_review",
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.approvals_reviewer = approvals_reviewer

    def capabilities(self) -> Capabilities:
        return Capabilities(
            schema_flag=True,
            read_only_mode=True,
            sessions=True,
            acp=False,
            streaming=True,
            skill_dirs=False,
            bypass=True,
            questions=True,
        )

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        return Invocation(
            argv=[binary, "app-server", *self.provider_args(), *self.extra_args]
        )

    def provider_args(self) -> list[str]:
        """``-c`` overrides that define and select ``self.provider``; the key itself is
        read from ``AM_PROVIDER_API_KEY`` (``env_key``), never put on the command line.
        The provider speaks the Responses API, the only ``wire_api`` Codex still loads
        (0.167.0 refuses ``chat``); a known context window of the model goes in
        ``model_context_window``."""
        p = self.provider
        if p is None:
            return []
        key = f"model_providers.am-{p.id}"
        pairs: list[tuple[str, Any]] = [
            ("model_provider", f"am-{p.id}"),
            (f"{key}.name", p.name or p.id),
            (f"{key}.base_url", p.base_url),
            (f"{key}.wire_api", "responses"),
        ]
        if p.api_key:
            pairs.append((f"{key}.env_key", "AM_PROVIDER_API_KEY"))
        spec = p.spec(self.model or (p.models[0] if p.models else None))
        if spec is not None and spec.context_window:
            pairs.append(("model_context_window", spec.context_window))
        args: list[str] = []
        for name, value in pairs:
            args += ["-c", f"{name}={json.dumps(value)}"]
        return args

    def provider_env(self) -> dict[str, str | None]:
        if self.provider is None or not self.provider.api_key:
            return {}
        return {"AM_PROVIDER_API_KEY": self.provider.api_key}

    def conversation(self, task: AgentTask, files: RunFiles) -> Conversation:
        return CodexConversation(self, task)

    def thread_settings(self, task: AgentTask) -> dict[str, Any]:
        if task.permissions == "bypass":
            return {"sandbox": "danger-full-access", "approvalPolicy": "never"}
        sandbox = "read-only" if task.mode == "read_only" else "workspace-write"
        if task.permissions == "restricted":
            return {"sandbox": sandbox, "approvalPolicy": "never"}
        return {
            "sandbox": sandbox,
            "approvalPolicy": "on-request",
            "approvalsReviewer": self.approvals_reviewer,
        }


class CodexConversation(Conversation):
    """One app-server thread and turn for one task."""

    def __init__(self, runtime: CodexRuntime, task: AgentTask) -> None:
        self.runtime = runtime
        self.task = task
        self.next_id = 0
        self.pending: dict[str, str] = {}
        self.thread_id: str | None = None

    # -- Conversation ------------------------------------------------------------------

    def opening(self) -> list[str]:
        return [
            self._request(
                "initialize",
                {"clientInfo": {"name": "auto-maintainer", "version": "0.1.0"}},
            )
        ]

    def feed(self, line: str) -> Step:
        msg = loads_object(line)
        if msg is None:
            return Step()
        if "method" in msg and "id" in msg:
            return self._server_request(msg)
        if "method" in msg:
            return self._notification(msg["method"], msg.get("params") or {})
        if "id" in msg:
            return self._response(msg)
        return Step()

    def answer(self, ask: Ask, answer: AgentAnswer) -> list[str]:
        request_id, method, params = ask.token
        if method == "item/tool/requestUserInput":
            if answer.allow:
                answers = {
                    qid: {"answers": list(values)}
                    for qid, values in answer.answers.items()
                }
            else:
                answers = {}
            result: dict[str, Any] = {"answers": answers}
        elif method == "item/permissions/requestApproval":
            granted = {
                k: v
                for k, v in (params.get("permissions") or {}).items()
                if k in ("fileSystem", "network")
            }
            result = {"permissions": granted if answer.allow else {}, "scope": "turn"}
        else:
            decision = answer.option_id if answer.option_id in _DECISIONS else None
            if (
                decision is None
                or (_DECISIONS[decision][1] in ("allow", "allow_session"))
                != answer.allow
            ):
                decision = "accept" if answer.allow else "decline"
            result = {"decision": decision}
        return [_line({"jsonrpc": "2.0", "id": request_id, "result": result})]

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
            text = str((msg.get("error") or {}).get("message") or msg.get("error"))
            return Step(
                events=[
                    AgentEvent(type=AgentEventType.ERROR, text=text),
                    partial(
                        is_error=True,
                        error=f"{method} failed: {text}",
                        error_kind=classify_failure(None, "", text),
                    ),
                ],
                done=True,
            )
        result = msg.get("result") or {}
        if method == "initialize":
            settings = {
                "cwd": str(self.task.workdir),
                **self.runtime.thread_settings(self.task),
            }
            model = self.task.model or self.runtime.model
            if model:
                settings["model"] = model
            if self.task.resume_session:
                start = self._request(
                    "thread/resume", {"threadId": self.task.resume_session, **settings}
                )
            else:
                start = self._request("thread/start", settings)
            return Step(
                send=[_line({"jsonrpc": "2.0", "method": "initialized"}), start]
            )
        if method in ("thread/start", "thread/resume"):
            self.thread_id = str(
                (result.get("thread") or {}).get("id") or self.task.resume_session or ""
            )
            params: dict[str, Any] = {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": _with_skill_dirs(self.task)}],
            }
            if self.task.output_schema is not None:
                params["outputSchema"] = strict_schema(self.task.output_schema)
            if self.task.effort:
                params["effort"] = _EFFORTS.get(self.task.effort, self.task.effort)
            return Step(
                events=[partial(session=self.thread_id)],
                send=[self._request("turn/start", params)],
            )
        return Step()

    def _notification(self, method: str, params: dict[str, Any]) -> Step:
        if method == "item/completed":
            return Step(events=_item_events("completed", params.get("item") or {}))
        if method == "item/started":
            return Step(events=_item_events("started", params.get("item") or {}))
        if method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            return Step(
                events=[
                    partial(
                        usage={
                            "input_tokens": int(total.get("inputTokens") or 0),
                            "cached_input_tokens": int(
                                total.get("cachedInputTokens") or 0
                            ),
                            "output_tokens": int(total.get("outputTokens") or 0)
                            + int(total.get("reasoningOutputTokens") or 0),
                        }
                    )
                ]
            )
        if method == "turn/completed":
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "")
            if status == "completed":
                return Step(done=True)
            message = str(
                (turn.get("error") or {}).get("message") or f"codex turn {status}"
            )
            return Step(
                events=[
                    AgentEvent(type=AgentEventType.ERROR, text=message),
                    partial(
                        is_error=True,
                        error=message,
                        error_kind=classify_failure(None, "", message),
                    ),
                ],
                done=True,
            )
        if method == "error" and not params.get("willRetry"):
            message = str((params.get("error") or {}).get("message") or "codex error")
            return Step(events=[AgentEvent(type=AgentEventType.ERROR, text=message)])
        return Step()

    def _server_request(self, msg: dict[str, Any]) -> Step:
        method = str(msg.get("method"))
        params = msg.get("params") or {}
        request_id = msg["id"]
        question: Question | None = None
        if method == "item/commandExecution/requestApproval":
            question = Question(
                kind=QuestionKind.PERMISSION,
                title="run a command"
                + (
                    f": {short(params.get('reason'), 200)}"
                    if params.get("reason")
                    else ""
                ),
                detail=str(params.get("command") or ""),
                tool="execute",
                paths=[str(params["cwd"])] if params.get("cwd") else [],
                options=_options(params.get("availableDecisions")),
            )
        elif method == "item/fileChange/requestApproval":
            question = Question(
                kind=QuestionKind.PERMISSION,
                title="change files"
                + (
                    f": {short(params.get('reason'), 200)}"
                    if params.get("reason")
                    else ""
                ),
                detail=str(params.get("grantRoot") or params.get("reason") or ""),
                tool="edit",
                paths=[str(params["grantRoot"])] if params.get("grantRoot") else [],
                options=_options(None),
            )
        elif method == "item/permissions/requestApproval":
            question = Question(
                kind=QuestionKind.PERMISSION,
                title="get more permissions"
                + (
                    f": {short(params.get('reason'), 200)}"
                    if params.get("reason")
                    else ""
                ),
                detail=short(params.get("permissions"), 4000),
                tool="permissions",
                options=[
                    QuestionOption(
                        id="accept", label="Allow for this turn", kind="allow"
                    ),
                    QuestionOption(id="decline", label="Deny", kind="deny"),
                ],
            )
        elif method == "item/tool/requestUserInput":
            inputs = [
                InputQuestion(
                    id=str(q.get("id")),
                    question=str(q.get("question") or ""),
                    header=str(q.get("header") or ""),
                    options=[
                        str(o.get("label"))
                        for o in q.get("options") or []
                        if isinstance(o, dict)
                    ],
                    free_text=bool(q.get("isOther")) or not q.get("options"),
                )
                for q in params.get("questions") or []
                if isinstance(q, dict) and not q.get("isSecret")
            ]
            question = Question(
                kind=QuestionKind.INPUT,
                title=short(inputs[0].question if inputs else "answer a question", 200),
                inputs=inputs,
            )
        elif method == "mcpServer/elicitation/request":
            return Step(
                send=[
                    _line(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"action": "decline"},
                        }
                    )
                ]
            )
        if question is None:
            return Step(
                send=[
                    _line(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": f"{method} is not supported",
                            },
                        }
                    )
                ]
            )
        return Step(asks=[Ask(question=question, token=(request_id, method, params))])


def _options(available: Any) -> list[QuestionOption]:
    names = [d for d in available or [] if isinstance(d, str) and d in _DECISIONS]
    if not names:
        names = ["accept", "decline"]
    if "decline" not in names:
        names.append("decline")
    return [
        QuestionOption(id=n, label=_DECISIONS[n][0], kind=_DECISIONS[n][1])
        for n in names
    ]  # type: ignore[arg-type]


def _item_events(stage: str, item: dict[str, Any]) -> list[AgentEvent]:
    itype = item.get("type")
    if itype == "agentMessage" and stage == "completed":
        text = str(item.get("text") or "")
        return (
            [AgentEvent(type=AgentEventType.MESSAGE, text=text), partial(text=text)]
            if text
            else []
        )
    if itype == "commandExecution":
        return [
            AgentEvent(
                type=AgentEventType.TOOL_CALL
                if stage == "started"
                else AgentEventType.TOOL_RESULT,
                text=str(item.get("command") or ""),
                data={
                    "id": item.get("id"),
                    "exit_code": item.get("exitCode"),
                    "status": item.get("status"),
                    "output": short(item.get("aggregatedOutput") or "", 500),
                },
            )
        ]
    if itype in ("fileChange", "mcpToolCall", "webSearch") and stage == "started":
        return [
            AgentEvent(
                type=AgentEventType.TOOL_CALL,
                text=str(itype),
                data={"id": item.get("id")},
            )
        ]
    return []


def _with_skill_dirs(task: AgentTask) -> str:
    dirs = [str(d) for d in [*task.skill_dirs, *task.extra_dirs, *task.reference_dirs]]
    if not dirs:
        return task.prompt
    listing = "\n".join(f"- {d}" for d in dirs)
    return (
        "Skills and reference material for this task are in these directories; read the "
        f"SKILL.md files there when they apply:\n{listing}\n\n{task.prompt}"
    )


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)
