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
        # A review someone asked for: its questions go to people.
        "requested_by": "maria",
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


async def test_a_background_read_only_task_is_not_left_waiting_on_people(
    tmp_path: Path,
) -> None:
    """Triage, scoring, and scheduled reviews have nobody waiting for them: what the
    automatic rules do not settle is denied at once instead of becoming a question
    that holds the run until it expires."""
    from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
    from auto_maintainer.domain.agents import AgentQuestion

    runtime = AntigravityRuntime(runs_dir=tmp_path / "runs")
    desk = FakeDesk(AgentAnswer(allow=True, by="maria"))
    runtime.questions = desk
    question = AgentQuestion(
        kind=QuestionKind.PERMISSION, title="run", tool="execute", detail="pytest -q"
    )
    background = _task(tmp_path, requested_by=None, kind=TaskKind.EVALUATE)
    answer = await runtime.resolve(background, question)
    assert answer.allow is False and "background" in (answer.message or "")
    assert desk.questions == []
    requested = await runtime.resolve(_task(tmp_path), question)
    assert requested.allow is True and len(desk.questions) == 1


async def test_an_agent_that_stops_on_a_refusal_is_told_to_go_on(
    tmp_path: Path, talking_cli
) -> None:
    """Antigravity ends its turn when a request is refused, waiting for a person; the
    conversation tells it what was refused and to finish without it."""
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
                    _permission("p1", "execute", "pytest", {"CommandLine": "pytest -q"})
                ],
            },
            {
                "wait": '"p1"',
                "emit": [
                    {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}}
                ],
            },
            {
                "wait": "do not try it again",
                "emit": [
                    _chunk('{"ok": true}'),
                    {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}},
                ],
            },
        ]
    )
    rt = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    task = _task(tmp_path, output_schema=SCHEMA, requested_by=None)
    _, result = await _run(rt, task)
    assert result.ok and result.structured == {"ok": True}, result.error
    prompts = _sent(cli, "method", "session/prompt")
    assert len(prompts) == 2
    again = prompts[1]["params"]["prompt"][0]["text"]
    assert "pytest -q" in again and "do not try it again" in again


# -- messages from reviewers while the agent works ---------------------------------------


def _thought(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


async def _run_telling(runtime, task, inbox, message: str):
    """Run ``task`` and send ``message`` once the agent is at work."""
    from auto_maintainer.agents.exec import Told

    events, told = [], False
    async for event in runtime.run(task):
        if not told and event.type in (AgentEventType.THINKING, AgentEventType.MESSAGE):
            told = inbox.put(task.job_id, Told(by="maria", text=message))
        events.append(event)
    return events[:-1], events[-1].result, told


async def test_a_message_cancels_an_acp_turn_and_starts_the_next(
    tmp_path: Path, talking_cli
) -> None:
    from auto_maintainer.agents.exec import Inbox

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
            {"wait": '"session/prompt"', "emit": [_thought("Reading the diff")]},
            {
                "wait": '"session/cancel"',
                "emit": [
                    {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "cancelled"}}
                ],
            },
            {
                "wait": "check the docs too",
                "emit": [
                    _chunk('{"ok": true}'),
                    {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}},
                ],
            },
        ]
    )
    inbox = Inbox()
    rt = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    rt.inbox = inbox
    task = _task(tmp_path, output_schema=SCHEMA, job_id="job1", messages=True)
    events, result, told = await _run_telling(rt, task, inbox, "check the docs too")
    assert told and result.ok and result.structured == {"ok": True}, result.error
    prompts = _sent(cli, "method", "session/prompt")
    assert len(prompts) == 2 and prompts[1]["params"]["sessionId"] == "s1"
    assert prompts[1]["params"]["prompt"][0]["text"] == "check the docs too"
    assert any(e.data.get("told_by") == "maria" for e in events)
    assert not inbox.listening("job1"), "the box closes with the run"


async def test_claude_reads_a_message_when_its_turn_ends(
    tmp_path: Path, talking_cli
) -> None:
    from auto_maintainer.agents.exec import Inbox

    first = {**RESULT, "result": '{"ok": false}'}
    cli = talking_cli(
        [
            {
                "wait": '"type": "user"',
                "emit": [
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Reading"}]},
                    }
                ],
            },
            {"sleep": 0.5, "emit": [first]},
            {"wait": "check the docs too", "emit": [RESULT]},
        ]
    )
    inbox = Inbox()
    rt = ClaudeCodeRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    rt.inbox = inbox
    task = _task(tmp_path, output_schema=SCHEMA, job_id="job1", messages=True)
    _, result, told = await _run_telling(rt, task, inbox, "check the docs too")
    assert told and result.ok and result.structured == {"ok": True}, result.error
    users = _sent(cli, "type", "user")
    assert [u["message"]["content"] for u in users][1:] == ["check the docs too"]


async def test_a_task_that_does_not_listen_takes_no_message(
    tmp_path: Path, talking_cli
) -> None:
    from auto_maintainer.agents.exec import Inbox

    cli = talking_cli(
        [
            {
                "wait": '"type": "user"',
                "emit": [
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Reading"}]},
                    }
                ],
            },
            {"sleep": 0.3, "emit": [RESULT]},
        ]
    )
    inbox = Inbox()
    rt = ClaudeCodeRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    rt.inbox = inbox
    task = _task(tmp_path, output_schema=SCHEMA, job_id="job1")
    _, result, told = await _run_telling(rt, task, inbox, "check the docs too")
    assert result.ok and not told, "only a task with messages set opens a box"


# -- long prompts and the agent's own question tool ------------------------------------


def test_a_prompt_longer_than_the_cli_takes_points_at_the_whole_task(
    tmp_path: Path,
) -> None:
    from auto_maintainer.agents.acp import AcpConversation
    from auto_maintainer.agents.exec import RunFiles

    runtime = AntigravityRuntime(runs_dir=tmp_path)
    runtime.max_prompt_bytes = 2_000  # type: ignore[misc]
    files = RunFiles.create(tmp_path / "runs", "run_long")
    prompt = "Answer the newest message: run the tests.\n" + "context line\n" * 1_000
    talk = AcpConversation(runtime, _task(tmp_path, prompt=prompt), files)
    talk.session_id = "s1"
    [line] = talk._next_setup()
    sent = json.loads(line)["params"]["prompt"][0]["text"]
    assert len(sent.encode()) <= 2_000 and sent.startswith("Answer the newest message")
    assert str(files.dir / "task.md") in sent
    assert (files.dir / "task.md").read_text() == prompt
    short = AcpConversation(runtime, _task(tmp_path, prompt="Short."), files)
    short.session_id = "s1"
    assert json.loads(short._next_setup()[0])["params"]["prompt"][0]["text"] == "Short."


async def test_a_question_the_agent_asks_with_its_own_tool_reaches_a_person(
    tmp_path: Path, talking_cli
) -> None:
    """Antigravity asks with its own ``ask_question`` tool, which ends its turn; the
    question goes to the desk, even in bypass mode, and the answer starts the next
    turn of the same session."""
    ask = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-1",
                "name": "ask_question",
                "title": "Run ask_question",
                "args": {
                    "questions": [
                        {
                            "question": "Which machine should run it?",
                            "options": ["a800", "none"],
                            "is_multi_select": False,
                        }
                    ]
                },
            },
        },
    }
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
                "wait": '"session/prompt"',
                "emit": [
                    ask,
                    _chunk("Which machine should run it?"),
                    {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
                ],
            },
            {
                "wait": "maria answered your question",
                "emit": [
                    _chunk('{"ok": true}'),
                    {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}},
                ],
            },
        ]
    )
    desk = FakeDesk(AgentAnswer(allow=True, answers={"0": ["a800"]}, by="maria"))
    rt = AntigravityRuntime(
        binary=str(cli.path),
        runs_dir=tmp_path / "runs",
        questions=desk,
        allow_bypass=True,
    )
    task = _task(tmp_path, output_schema=SCHEMA, permissions="bypass")
    _, result = await _run(rt, task)
    assert result.ok and result.structured == {"ok": True}, result.error
    [question] = desk.questions
    assert question.kind == QuestionKind.INPUT and question.asked_for == "maria"
    assert question.inputs[0].options == ["a800", "none"]
    answer = _sent(cli, "method", "session/prompt")[-1]["params"]["prompt"][0]["text"]
    assert "Q: Which machine should run it?\nA: a800" in answer


async def test_a_question_is_withdrawn_when_its_agent_stops_waiting(
    tmp_path: Path, talking_cli
) -> None:
    """The agent's container ends while a person is being asked: the question is
    withdrawn rather than left for an answer nothing would read, and the run fails
    as a crash, not as invalid output that a repair turn would retry blind."""
    ask = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-1",
                "name": "ask_question",
                "args": {"questions": [{"question": "Which scope?", "options": ["a"]}]},
            },
        },
    }
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
                    ask,
                    {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}},
                ],
            },
            {"sleep": 0.3, "die": True},
        ]
    )

    class SlowDesk:
        def __init__(self) -> None:
            self.withdrawn = False

        async def ask(self, question: Any, timeout_seconds: float) -> Any:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.withdrawn = True
                raise

    desk = SlowDesk()
    rt = AntigravityRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", questions=desk
    )
    _, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA))
    assert desk.withdrawn, "the question is withdrawn from the desk"
    assert not result.ok and result.error_kind == "crash"
    assert "stopped while it waited for an answer to: Which scope?" in result.error


def _replayed(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s0",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


async def test_an_acp_agent_resumes_its_session_after_a_restart(
    tmp_path: Path, talking_cli
) -> None:
    """A job that a restart cut off runs again with the agent's own session: the
    ACP server loads it (replaying its history, which is not this run's work) and
    the prompt continues it."""
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": True},
                        },
                    }
                ],
            },
            {
                "wait": '"session/load"',
                "emit": [
                    _replayed("Installing the environment, as before the restart."),
                    {"jsonrpc": "2.0", "id": 2, "result": {}},
                ],
            },
            {
                "wait": '"session/set_mode"',
                "emit": [{"jsonrpc": "2.0", "id": 3, "result": {}}],
            },
            {
                "wait": '"session/prompt"',
                "emit": [
                    _replayed('{"ok": true}'),
                    {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}},
                ],
            },
        ]
    )
    rt = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    events, result = await _run(
        rt, _task(tmp_path, output_schema=SCHEMA, resume_session="s0")
    )
    assert result.ok and result.structured == {"ok": True} and result.session == "s0"
    [load] = _sent(cli, "method", "session/load")
    assert load["params"]["sessionId"] == "s0"
    assert not _sent(cli, "method", "session/new")
    texts = " ".join(e.text for e in events)
    assert "as before the restart" not in texts, "the replayed history is not reported"
    assert [e.data["session"] for e in events if e.type == "session"] == ["s0"]


async def test_an_acp_session_that_cannot_load_starts_afresh(
    tmp_path: Path, talking_cli
) -> None:
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": True},
                        },
                    }
                ],
            },
            {
                "wait": '"session/load"',
                "emit": [
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "error": {"code": -32602, "message": "no such session"},
                    }
                ],
            },
            {
                "wait": '"session/new"',
                "emit": [{"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "s1"}}],
            },
            {
                "wait": '"session/set_mode"',
                "emit": [{"jsonrpc": "2.0", "id": 4, "result": {}}],
            },
            {
                "wait": '"session/prompt"',
                "emit": [
                    _replayed('{"ok": true}'),
                    {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}},
                ],
            },
        ]
    )
    rt = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    events, result = await _run(
        rt, _task(tmp_path, output_schema=SCHEMA, resume_session="s0")
    )
    assert result.ok and result.session == "s1"
    assert any("could not be resumed" in e.text for e in events)
    [prompt] = _sent(cli, "method", "session/prompt")
    assert prompt["params"]["prompt"][0]["text"].startswith("Review it."), (
        "the fresh session gets the whole task"
    )
