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

"""Agents that talk back: questions reach the desk, answers reach the CLI.

The fake CLIs replay the message shapes observed from Claude Code 2.1.318
(``--permission-prompt-tool stdio``), the Codex 0.162.1 app server, and the ACP
server of Antigravity 1.5.1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.domain.agents import (
    AgentAnswer,
    AgentEventType,
    AgentTask,
    QuestionKind,
    TaskKind,
)


class FakeDesk:
    """Implements ``AgentQuestions``: records questions and answers from a script."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.questions: list[Any] = []
        self.timeouts: list[float] = []

    async def ask(self, question: Any, timeout_seconds: float) -> Any:
        self.questions.append(question)
        self.timeouts.append(timeout_seconds)
        return self.answers.pop(0)


SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def _task(tmp_path: Path, **kw: object) -> AgentTask:
    data: dict[str, object] = {
        "kind": TaskKind.REVIEW,
        "prompt": "Review it.",
        "workdir": tmp_path,
        "repo": "acme/widget",
        "pr": 42,
    }
    data.update(kw)
    return AgentTask.model_validate(data)


async def _run(runtime, task):
    events = [e async for e in runtime.run(task)]
    return events[:-1], events[-1].result


def _sent(cli, key: str, value: str) -> list[dict]:
    return [m for m in cli.received() if m.get(key) == value]


# -- Claude Code -----------------------------------------------------------------------

CAN_USE_TOOL = {
    "type": "control_request",
    "request_id": "req-7",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "input": {"command": "echo hi > out.txt", "description": "Write hi"},
        "description": "Write hi",
        "blocked_path": "/w/out.txt",
    },
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": '{"ok": true}',
    "session_id": "s1",
}


async def test_claude_prompt_goes_to_a_reviewer(tmp_path: Path, talking_cli) -> None:
    cli = talking_cli(
        [
            {"wait": '"type": "user"', "emit": [CAN_USE_TOOL]},
            {"wait": '"type": "control_response"', "emit": [RESULT]},
        ]
    )
    desk = FakeDesk(AgentAnswer(allow=True, by="maria"))
    rt = ClaudeCodeRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    events, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA))
    assert result.ok and result.structured == {"ok": True}
    [question] = desk.questions
    assert question.kind == QuestionKind.PERMISSION and question.tool == "Bash"
    assert question.detail == "echo hi > out.txt" and "/w/out.txt" in question.paths
    assert (question.repo, question.pr, question.runtime) == (
        "acme/widget",
        42,
        "claude-code",
    )
    assert desk.timeouts == [1800]
    [reply] = _sent(cli, "type", "control_response")
    assert reply["response"]["request_id"] == "req-7"
    assert reply["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": CAN_USE_TOOL["request"]["input"],
    }
    assert any(
        e.type == AgentEventType.PERMISSION_REQUEST and e.data["by"] == "maria"
        for e in events
    )


async def test_claude_denial_carries_the_reviewers_reason(
    tmp_path: Path, talking_cli
) -> None:
    cli = talking_cli(
        [
            {"wait": '"type": "user"', "emit": [CAN_USE_TOOL]},
            {"wait": '"type": "control_response"', "emit": [RESULT]},
        ]
    )
    desk = FakeDesk(AgentAnswer(allow=False, message="not in a review", by="maria"))
    rt = ClaudeCodeRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    await _run(rt, _task(tmp_path))
    [reply] = _sent(cli, "type", "control_response")
    assert reply["response"]["response"] == {
        "behavior": "deny",
        "message": "not in a review",
    }


async def test_restricted_runs_deny_without_asking(tmp_path: Path, talking_cli) -> None:
    cli = talking_cli(
        [
            {"wait": '"type": "user"', "emit": [CAN_USE_TOOL]},
            {"wait": '"type": "control_response"', "emit": [RESULT]},
        ]
    )
    desk = FakeDesk()
    rt = ClaudeCodeRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    await _run(rt, _task(tmp_path, permissions="restricted"))
    assert desk.questions == []
    [reply] = _sent(cli, "type", "control_response")
    assert reply["response"]["response"]["behavior"] == "deny"


async def test_waiting_for_a_reviewer_does_not_count_against_the_timeout(
    tmp_path: Path, talking_cli
) -> None:
    class SlowDesk(FakeDesk):
        async def ask(self, question, timeout_seconds):
            await asyncio.sleep(1.5)
            return await super().ask(question, timeout_seconds)

    cli = talking_cli(
        [
            {"wait": '"type": "user"', "emit": [CAN_USE_TOOL]},
            {"wait": '"type": "control_response"', "emit": [RESULT]},
        ]
    )
    desk = SlowDesk(AgentAnswer(allow=True, by="maria"))
    rt = ClaudeCodeRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    _, result = await _run(rt, _task(tmp_path, timeout_seconds=1))
    assert result.ok, result.error


# -- Codex app server --------------------------------------------------------------------


def _codex_steps(server_request: dict, answer_marker: str) -> list[dict]:
    return [
        {"wait": '"initialize"', "emit": [{"id": 1, "result": {"userAgent": "codex"}}]},
        {
            "wait": '"thread/start"',
            "emit": [{"id": 2, "result": {"thread": {"id": "t1"}}}],
        },
        {
            "wait": '"turn/start"',
            "emit": [
                {"id": 3, "result": {"turn": {"id": "u1", "status": "inProgress"}}},
                server_request,
            ],
        },
        {
            "wait": answer_marker,
            "emit": [
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "id": "m",
                            "text": '{"ok":true}',
                        }
                    },
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "total": {
                                "inputTokens": 100,
                                "outputTokens": 7,
                                "reasoningOutputTokens": 3,
                            }
                        }
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "u1", "status": "completed"}},
                },
            ],
        },
    ]


async def test_codex_user_input_goes_to_a_reviewer(tmp_path: Path, talking_cli) -> None:
    ask = {
        "id": 0,
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "itemId": "i",
            "isBlocking": True,
            "questions": [
                {
                    "id": "scope",
                    "header": "Scope",
                    "question": "Review the docs too?",
                    "options": [
                        {"label": "Yes", "description": "include docs"},
                        {"label": "No", "description": "code only"},
                    ],
                }
            ],
        },
    }
    cli = talking_cli(_codex_steps(ask, '"answers"'))
    desk = FakeDesk(AgentAnswer(allow=True, answers={"scope": ["Yes"]}, by="maria"))
    rt = CodexRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk)
    _, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA, effort="max"))
    assert result.ok and result.structured == {"ok": True} and result.session == "t1"
    assert result.usage.input_tokens == 100 and result.usage.output_tokens == 10
    [question] = desk.questions
    assert question.kind == QuestionKind.INPUT
    assert (
        question.inputs[0].options == ["Yes", "No"]
        and question.inputs[0].header == "Scope"
    )
    [start] = _sent(cli, "method", "thread/start")
    assert start["params"] == {
        "cwd": str(tmp_path),
        "sandbox": "read-only",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
    }
    [turn] = _sent(cli, "method", "turn/start")
    assert turn["params"]["effort"] == "xhigh"
    assert turn["params"]["outputSchema"]["additionalProperties"] is False
    assert _sent(cli, "method", "initialized")
    reply = next(m for m in cli.received() if m.get("id") == 0)
    assert reply["result"] == {"answers": {"scope": {"answers": ["Yes"]}}}


async def test_codex_approval_decisions(tmp_path: Path, talking_cli) -> None:
    ask = {
        "id": 5,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "itemId": "e",
            "startedAtMs": 1,
            "command": "curl https://example.com",
            "reason": "needs network",
            "availableDecisions": ["accept", "acceptForSession", "decline"],
        },
    }
    cli = talking_cli(_codex_steps(ask, '"decision"'))
    desk = FakeDesk(AgentAnswer(allow=False, option_id="decline", by="maria"))
    rt = CodexRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk)
    await _run(rt, _task(tmp_path))
    [question] = desk.questions
    assert (
        question.detail == "curl https://example.com"
        and "needs network" in question.title
    )
    assert [o.id for o in question.options] == ["accept", "acceptForSession", "decline"]
    reply = next(m for m in cli.received() if m.get("id") == 5)
    assert reply["result"] == {"decision": "decline"}


# -- ACP (Antigravity) ---------------------------------------------------------------------


def _permission(
    request_id: str, kind: str, title: str, raw: dict, locations=()
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {
                "toolCallId": f"call-{request_id}",
                "kind": kind,
                "title": title,
                "rawInput": raw,
                "locations": [{"path": p} for p in locations],
            },
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            ],
        },
    }


def _chunk(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


async def test_acp_settles_safe_requests_and_asks_for_the_rest(
    tmp_path: Path, talking_cli
) -> None:
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}],
            },
            {
                "wait": '"session/new"',
                "emit": [{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s1"}}],
            },
            {
                "wait": '"session/set_mode"',
                "emit": [{"jsonrpc": "2.0", "id": 3, "result": {}}],
            },
            {
                "wait": '"session/prompt"',
                "emit": [
                    _permission(
                        "p1", "execute", "git diff", {"CommandLine": "git diff HEAD~1"}
                    )
                ],
            },
            {
                "wait": '"p1"',
                "emit": [
                    _permission(
                        "p2", "execute", "curl", {"CommandLine": "curl https://x.test"}
                    )
                ],
            },
            {
                "wait": '"p2"',
                "emit": [
                    _permission(
                        "p3", "edit", "edit a file", {}, [str(tmp_path / "a.py")]
                    ),
                ],
            },
            {
                "wait": '"p3"',
                "emit": [
                    _chunk('{"ok": '),
                    _chunk("true}"),
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "result": {
                            "stopReason": "end_turn",
                            "usage": {"inputTokens": 10, "outputTokens": 2},
                        },
                    },
                ],
            },
        ]
    )
    desk = FakeDesk(
        AgentAnswer(allow=False, option_id="deny", by="maria", message="no network")
    )
    rt = AntigravityRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    events, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA))
    assert result.ok and result.structured == {"ok": True}, result.error
    assert result.usage.input_tokens == 10
    [question] = desk.questions
    assert question.detail == "curl https://x.test" and question.tool == "execute"
    replies = {
        m["id"]: m["result"]["outcome"]
        for m in cli.received()
        if m.get("id") in ("p1", "p2", "p3")
    }
    assert replies["p1"] == {"outcome": "selected", "optionId": "allow"}
    assert replies["p2"] == {"outcome": "selected", "optionId": "deny"}
    assert replies["p3"] == {"outcome": "selected", "optionId": "deny"}
    automatic = [
        e
        for e in events
        if e.type == AgentEventType.PERMISSION_REQUEST and e.data["automatic"]
    ]
    assert len(automatic) == 2
    [new] = _sent(cli, "method", "session/new")
    assert new["params"] == {"cwd": str(tmp_path), "mcpServers": []}
    [mode] = _sent(cli, "method", "session/set_mode")
    assert mode["params"] == {"sessionId": "s1", "modeId": "default"}
    [prompt] = _sent(cli, "method", "session/prompt")
    assert prompt["params"]["prompt"][0]["text"].startswith("Review it.")


async def test_acp_failure_to_start_a_session_ends_the_run(
    tmp_path: Path, talking_cli
) -> None:
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [{"jsonrpc": "2.0", "id": 1, "result": {}}],
            },
            {
                "wait": '"session/new"',
                "emit": [
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "error": {"code": -32000, "message": "Authentication required"},
                    }
                ],
            },
        ]
    )
    rt = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    _, result = await _run(rt, _task(tmp_path))
    assert not result.ok and result.error_kind == "auth"
    assert json.dumps(cli.argv())


def test_acp_thoughts_are_thinking_events_and_stay_out_of_the_answer(
    tmp_path: Path,
) -> None:
    from auto_maintainer.agents.acp import AcpConversation
    from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
    from auto_maintainer.domain.agents import AgentEventType, AgentTask

    conversation = AcpConversation(
        AntigravityRuntime(runs_dir=tmp_path),
        AgentTask(kind="review", prompt="x", workdir=tmp_path),
    )
    [event] = conversation._update(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "Checking the diff"},
        }
    )
    assert event.type == AgentEventType.THINKING and event.text == "Checking the diff"
    assert conversation.current == []


async def test_an_acp_cli_reports_its_models_and_levels_without_a_prompt(
    tmp_path: Path, talking_cli
) -> None:
    from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime

    session = {
        "sessionId": "s1",
        "configOptions": [
            {
                "id": "model",
                "category": "model",
                "currentValue": "gemini-4-argon",
                "options": [
                    {"value": "gemini-4-argon", "name": "Gemini 4 Argon"},
                    {"value": "claude-opus-5-5", "name": "Claude Opus 5.5"},
                ],
            },
            {
                "id": "thought_level",
                "category": "thought_level",
                "currentValue": "medium",
                "options": [{"value": v, "name": v} for v in ("low", "medium", "high")],
            },
        ],
    }
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}],
            },
            {
                "wait": '"session/new"',
                "emit": [{"jsonrpc": "2.0", "id": 2, "result": session}],
            },
            {"wait": '"session/prompt"', "emit": []},
        ]
    )
    runtime = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    choices = await runtime.choices()
    assert choices is not None and choices.source == "cli"
    assert [m.id for m in choices.models] == ["gemini-4-argon", "claude-opus-5-5"]
    assert choices.models[0].name == "Gemini 4 Argon"
    assert choices.default_model == "gemini-4-argon"
    assert choices.efforts == ["low", "medium", "high"]
    assert choices.default_effort == "medium"
    assert await runtime.choices() is choices, "kept for an hour"
