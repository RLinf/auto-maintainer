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

"""Adapters: command lines per permission mode, and parsing of captured Claude output."""

from __future__ import annotations

import json
from pathlib import Path

from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.agents.adapters.gemini import GeminiRuntime
from auto_maintainer.agents.adapters.generic import GenericRuntime
from auto_maintainer.agents.adapters.opencode import OpenCodeRuntime
from auto_maintainer.agents.exec import ExecRuntime, RunFiles
from auto_maintainer.domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    TaskKind,
)

FIXTURES = Path(__file__).parent / "fixtures"
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
    }
    data.update(kw)
    return AgentTask.model_validate(data)


def _files(tmp_path: Path) -> RunFiles:
    return RunFiles.create(tmp_path / "runs", "run_test")


async def _run(
    runtime: ExecRuntime, task: AgentTask
) -> tuple[list[AgentEvent], AgentResult]:
    events = [e async for e in runtime.run(task)]
    assert events[-1].type == AgentEventType.FINAL and events[-1].result is not None
    return events[:-1], events[-1].result


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# -- command lines ---------------------------------------------------------------------


def test_every_task_defaults_to_auto_mode(tmp_path: Path) -> None:
    assert _task(tmp_path).permissions == "auto"


def test_claude_auto_mode_hands_prompts_to_the_service(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        output_schema=SCHEMA,
        skill_dirs=[tmp_path / "skills"],
        model="opus",
        effort="high",
    )
    inv = ClaudeCodeRuntime().build_invocation(task, _files(tmp_path), "claude")
    argv = inv.argv
    assert argv[:7] == [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert _after(argv, "--permission-mode") == "auto"
    assert _after(argv, "--permission-prompt-tool") == "stdio"
    assert "--permission-prompts" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert _after(argv, "--tools") == "Read,Grep,Glob,Bash"
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--setting-sources")]
    assert "Bash(git diff:*)" in allowed and not any(
        a.startswith(("Edit", "Write")) for a in allowed
    )
    assert _after(argv, "--setting-sources") == ""
    assert "--strict-mcp-config" in argv
    assert json.loads(_after(argv, "--json-schema")) == SCHEMA
    assert _after(argv, "--add-dir") == str(tmp_path / "skills")
    assert _after(argv, "--model") == "opus" and _after(argv, "--effort") == "high"
    assert inv.stdin is None


def test_claude_restricted_and_bypass_modes(tmp_path: Path) -> None:
    rt = ClaudeCodeRuntime()
    restricted = rt.build_invocation(
        _task(tmp_path, permissions="restricted"), _files(tmp_path), "claude"
    ).argv
    assert _after(restricted, "--permission-prompts") == "none"
    assert _after(restricted, "--permission-mode") == "dontAsk"
    write = rt.build_invocation(
        _task(
            tmp_path,
            permissions="restricted",
            mode="workspace_write",
            resume_session="sess-1",
        ),
        _files(tmp_path),
        "claude",
    ).argv
    assert _after(write, "--permission-mode") == "acceptEdits"
    assert "--tools" not in write and _after(write, "--resume") == "sess-1"
    bypass = rt.build_invocation(
        _task(tmp_path, permissions="bypass"), _files(tmp_path), "claude"
    ).argv
    assert "--dangerously-skip-permissions" in bypass and "--tools" not in bypass


def test_codex_runs_the_app_server(tmp_path: Path) -> None:
    rt = CodexRuntime(extra_args=["-c", "x=1"])
    inv = rt.build_invocation(_task(tmp_path), _files(tmp_path), "codex")
    assert inv.argv == ["codex", "app-server", "-c", "x=1"] and inv.stdin is None
    auto = rt.thread_settings(_task(tmp_path))
    assert auto == {
        "sandbox": "read-only",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
    }
    write = rt.thread_settings(_task(tmp_path, mode="workspace_write"))
    assert write["sandbox"] == "workspace-write"
    assert rt.thread_settings(_task(tmp_path, permissions="restricted")) == {
        "sandbox": "read-only",
        "approvalPolicy": "never",
    }
    assert rt.thread_settings(_task(tmp_path, permissions="bypass"))["sandbox"] == (
        "danger-full-access"
    )
    assert (
        CodexRuntime(approvals_reviewer="user").thread_settings(_task(tmp_path))[
            "approvalsReviewer"
        ]
        == "user"
    )


def test_acp_runtimes_start_their_servers(tmp_path: Path) -> None:
    files = _files(tmp_path)
    agy = AntigravityRuntime()
    read_only = agy.build_invocation(
        _task(tmp_path, skill_dirs=[tmp_path / "s"]), files, "agy"
    ).argv
    assert read_only[:3] == ["agy", "acp", "--disable-telemetry"]
    assert "--sandbox" in read_only and _after(read_only, "--add-dir") == str(
        tmp_path / "s"
    )
    assert agy.session_setup(
        _task(tmp_path, model="gemini-3.8-flash", effort="max"), {}
    ) == [
        ("session/set_mode", {"modeId": "default"}),
        (
            "session/set_config_option",
            {"configId": "model", "value": "gemini-3.8-flash"},
        ),
        ("session/set_config_option", {"configId": "thought_level", "value": "high"}),
    ]
    assert agy.session_setup(_task(tmp_path, mode="workspace_write"), {}) == [
        ("session/set_mode", {"modeId": "accept-edits"})
    ]
    bypass = agy.build_invocation(_task(tmp_path, permissions="bypass"), files, "agy")
    assert "--dangerously-skip-permissions" in bypass.argv

    oc = OpenCodeRuntime()
    assert oc.build_invocation(_task(tmp_path), files, "opencode").argv == [
        "opencode",
        "acp",
    ]
    config = json.loads(oc.task_env(_task(tmp_path))["OPENCODE_CONFIG_CONTENT"])
    assert (
        config["permission"]["edit"] == "deny" and config["permission"]["bash"] == "ask"
    )
    write = json.loads(
        oc.task_env(_task(tmp_path, mode="workspace_write"))["OPENCODE_CONFIG_CONTENT"]
    )
    assert write["permission"]["edit"] == "ask"

    gm = GeminiRuntime().build_invocation(
        _task(tmp_path, extra_dirs=[tmp_path / "a", tmp_path / "b"]), files, "gemini"
    )
    assert gm.argv[:4] == ["gemini", "--experimental-acp", "--approval-mode", "default"]
    assert (
        _after(gm.argv, "--include-directories") == f"{tmp_path / 'a'},{tmp_path / 'b'}"
    )
    write = GeminiRuntime().build_invocation(
        _task(tmp_path, mode="workspace_write"), files, "gemini"
    )
    assert _after(write.argv, "--approval-mode") == "auto_edit"


def test_generic_placeholders_fill_once(tmp_path: Path) -> None:
    rt = GenericRuntime(
        "dsh",
        command=["dsh", "run", "--cwd", "{workdir}", "--prompt-file", "{prompt_file}"],
    )
    files = _files(tmp_path)
    inv = rt.build_invocation(_task(tmp_path), files, "/bin/dsh")
    assert inv.argv == [
        "/bin/dsh",
        "run",
        "--cwd",
        str(tmp_path),
        "--prompt-file",
        str(files.prompt),
    ]
    assert inv.stdin is None
    stdin_rt = GenericRuntime("x", command=["x", "--quiet"])
    assert (
        stdin_rt.build_invocation(
            _task(tmp_path, prompt="{prompt_file}"), files, "x"
        ).stdin
        == "{prompt_file}"
    )
    inline = GenericRuntime("y", command=["y", "{prompt}"])
    assert inline.build_invocation(
        _task(tmp_path, prompt="say {workdir}"), files, "y"
    ).argv == ["y", "say {workdir}"]


# -- parsing real output ---------------------------------------------------------------


async def test_claude_parses_captured_stream(tmp_path: Path, talking_cli) -> None:
    cli = talking_cli(
        [
            {
                "wait": '"type": "user"',
                "emit": fixture_text("claude_stream.jsonl").splitlines(),
            }
        ]
    )
    rt = ClaudeCodeRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    events, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA))
    assert (
        result.ok and result.structured == {"ok": True} and result.text == '{"ok":true}'
    )
    assert result.session == "1c698847-6c7d-422c-9f31-0433bf878052"
    assert (
        result.usage.cost_usd
        and result.usage.cost_usd > 0.1
        and result.usage.output_tokens == 163
    )
    assert any(
        e.type == AgentEventType.TOOL_CALL and e.text == "StructuredOutput"
        for e in events
    )
    assert any(e.type == AgentEventType.TOOL_RESULT for e in events)
    sent = cli.received()
    assert sent[0]["request"]["subtype"] == "initialize"
    assert sent[1] == {
        "type": "user",
        "message": {"role": "user", "content": "Review it."},
    }


async def test_claude_error_result_is_a_failure(tmp_path: Path, talking_cli) -> None:
    line = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Invalid API key · Please run /login",
        "session_id": "s",
    }
    cli = talking_cli([{"wait": '"type": "user"', "emit": [line]}], exit=1)
    rt = ClaudeCodeRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    _, result = await _run(rt, _task(tmp_path))
    assert (
        not result.ok
        and result.error_kind == "auth"
        and "Invalid API key" in (result.error or "")
    )


async def test_generic_json_with_final_path(tmp_path: Path, fake_cli) -> None:
    cli = fake_cli(stdout=json.dumps({"result": {"text": '{"ok": true}'}}))
    rt = GenericRuntime(
        "pi",
        command=[str(cli.path), "--json"],
        output_format="json",
        final_path="result.text",
        runs_dir=tmp_path / "runs",
    )
    _, result = await _run(rt, _task(tmp_path, output_schema=SCHEMA))
    assert (
        result.ok and result.structured == {"ok": True} and cli.stdin() == "Review it."
    )


async def test_probe_reports_version(tmp_path: Path, fake_cli) -> None:
    cli = fake_cli(stdout="2.1.318 (Claude Code)\n", read_stdin=False)
    health = await ClaudeCodeRuntime(binary=str(cli.path)).probe()
    assert (
        health.available
        and health.version == "2.1.318"
        and health.authenticated is None
    )
    missing = await ClaudeCodeRuntime(binary="definitely-not-installed-cli").probe()
    assert not missing.available
