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

"""Permission modes: auto by default, opt-in bypass, wrappers, and credential isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.agents.adapters.gemini import GeminiRuntime
from auto_maintainer.agents.adapters.generic import GenericRuntime
from auto_maintainer.agents.adapters.opencode import OpenCodeRuntime
from auto_maintainer.agents.exec import RunFiles, isolation_env
from auto_maintainer.agents.registry import RuntimeConfig, build_runtime
from auto_maintainer.agents.router import effective_permissions
from auto_maintainer.domain.agents import AgentEventType, AgentTask, TaskKind
from auto_maintainer.domain.profile import Route


def _task(tmp_path: Path, **kw: object) -> AgentTask:
    data: dict[str, object] = {
        "kind": TaskKind.REVIEW,
        "prompt": "Review it.",
        "workdir": tmp_path,
    }
    data.update(kw)
    return AgentTask.model_validate(data)


def _argv(runtime, task: AgentTask, tmp_path: Path) -> list[str]:
    files = RunFiles.create(tmp_path / "runs", task.id)
    return runtime.build_invocation(task, files, runtime.binary).argv


def test_each_cli_gets_its_own_bypass_flag(tmp_path: Path) -> None:
    bypass = _task(tmp_path, permissions="bypass")
    claude = _argv(ClaudeCodeRuntime(), bypass, tmp_path)
    assert "--dangerously-skip-permissions" in claude
    assert "--permission-mode" not in claude and "--tools" not in claude
    codex = CodexRuntime().thread_settings(bypass)
    assert codex == {"sandbox": "danger-full-access", "approvalPolicy": "never"}
    agy = _argv(AntigravityRuntime(), bypass, tmp_path)
    assert "--dangerously-skip-permissions" in agy and "--sandbox" not in agy
    gemini = _argv(GeminiRuntime(), bypass, tmp_path)
    assert gemini[gemini.index("--approval-mode") + 1] == "yolo"
    opencode = OpenCodeRuntime().task_env(bypass)["OPENCODE_CONFIG_CONTENT"]
    assert set(json.loads(opencode)["permission"].values()) == {"allow"}
    generic = GenericRuntime(
        "dsh", command=["dsh", "run", "{prompt}"], bypass_args=["--yes"]
    )
    assert _argv(generic, bypass, tmp_path)[-1] == "--yes"
    assert generic.capabilities().bypass
    assert not GenericRuntime("pi", command=["pi", "{prompt}"]).capabilities().bypass


def test_auto_mode_keeps_each_clis_own_checks(tmp_path: Path) -> None:
    task = _task(tmp_path)
    claude = _argv(ClaudeCodeRuntime(), task, tmp_path)
    assert "--dangerously-skip-permissions" not in claude
    assert claude[claude.index("--permission-mode") + 1] == "auto"
    assert "--sandbox" in _argv(AntigravityRuntime(), task, tmp_path)
    assert CodexRuntime().thread_settings(task)["sandbox"] == "read-only"


def test_claude_marks_its_sandbox_only_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert ClaudeCodeRuntime().bypass_env() == {"IS_SANDBOX": "1"}
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert ClaudeCodeRuntime().bypass_env() == {}


@pytest.mark.parametrize(
    ("route", "allow", "untrusted", "expected"),
    [
        (Route(primary="x"), True, True, "auto"),
        (Route(primary="x"), True, False, "auto"),
        (Route(primary="x", permissions="restricted"), True, False, "restricted"),
        (Route(primary="x", permissions="bypass"), False, False, "auto"),
        (Route(primary="x", permissions="bypass"), True, True, "auto"),
        (Route(primary="x", permissions="bypass"), True, False, "bypass"),
        (
            Route(primary="x", permissions="bypass", bypass_untrusted=True),
            True,
            True,
            "bypass",
        ),
    ],
)
def test_bypass_needs_the_route_the_operator_and_trusted_input(
    tmp_path: Path, route: Route, allow: bool, untrusted: bool, expected: str
) -> None:
    runtime = build_runtime(
        "claude-code", RuntimeConfig(adapter="claude-code", allow_bypass=allow)
    )
    task = _task(tmp_path, untrusted_input=untrusted)
    assert effective_permissions(route, runtime, task) == expected


@pytest.mark.parametrize(
    ("route", "own", "untrusted", "expected"),
    [
        # the agent's own bypass needs no allow_bypass, but only team PRs get it
        (Route(primary="x"), "bypass", False, "bypass"),
        (Route(primary="x"), "bypass", True, "auto"),
        (Route(primary="x", bypass_untrusted=True), "bypass", True, "bypass"),
        # the agent's own mode wins over the route's either way
        (Route(primary="x", permissions="bypass"), "restricted", False, "restricted"),
        (Route(primary="x", permissions="restricted"), "auto", False, "auto"),
        (Route(primary="x", permissions="restricted"), None, False, "restricted"),
    ],
)
def test_an_agent_can_have_its_own_mode_for_every_task(
    tmp_path: Path, route: Route, own: str | None, untrusted: bool, expected: str
) -> None:
    runtime = build_runtime(
        "antigravity", RuntimeConfig(adapter="antigravity", permissions=own)
    )
    task = _task(tmp_path, untrusted_input=untrusted)
    mode = effective_permissions(route, runtime, task)
    assert mode == expected
    argv = _argv(runtime, task.model_copy(update={"permissions": mode}), tmp_path)
    assert ("--dangerously-skip-permissions" in argv) is (expected == "bypass")


async def test_an_agent_set_to_bypass_runs_without_allow_bypass(
    tmp_path: Path, fake_cli
) -> None:
    cli = fake_cli(
        stdout='{"type":"result","subtype":"success","result":"ok","session_id":"s"}\n',
        read_stdin=False,
    )
    runtime = ClaudeCodeRuntime(
        binary=str(cli.path), runs_dir=tmp_path / "runs", permissions="bypass"
    )
    events = [e async for e in runtime.run(_task(tmp_path, permissions="bypass"))]
    assert events[-1].type == AgentEventType.FINAL
    assert "--dangerously-skip-permissions" in cli.argv()


def test_defaults_never_bypass() -> None:
    from auto_maintainer.agents.registry import DEFAULT_RUNTIMES

    route = Route(primary="x")
    assert route.permissions == "auto" and not route.bypass_untrusted
    for name in ("claude-code", "codex", "antigravity", "opencode", "gemini"):
        assert not DEFAULT_RUNTIMES[name].allow_bypass, name
        assert DEFAULT_RUNTIMES[name].permissions is None, name
        runtime = build_runtime(name, DEFAULT_RUNTIMES[name])
        assert runtime.capabilities().bypass and runtime.capabilities().questions, name


async def test_runtime_refuses_bypass_the_operator_did_not_allow(
    tmp_path: Path, fake_cli
) -> None:
    cli = fake_cli(
        stdout='{"type":"result","subtype":"success","result":"ok","session_id":"s"}\n',
        read_stdin=False,
    )
    runtime = ClaudeCodeRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    events = [e async for e in runtime.run(_task(tmp_path, permissions="bypass"))]
    assert events[-1].type == AgentEventType.FINAL
    assert "--dangerously-skip-permissions" not in cli.argv()


async def test_prefix_wraps_the_command_and_isolation_hides_credentials(
    tmp_path: Path, fake_cli
) -> None:
    cli = fake_cli(
        stdout='{"type":"result","subtype":"success","result":"ok","session_id":"s"}\n',
        read_stdin=False,
    )
    runtime = ClaudeCodeRuntime(
        binary="claude",
        runs_dir=tmp_path / "runs",
        allow_bypass=True,
        command_prefix=[str(cli.path), "--wrapped-in", "{workdir}", "--"],
    )
    [*_, final] = [e async for e in runtime.run(_task(tmp_path, permissions="bypass"))]
    argv = cli.argv()
    assert argv[:3] == ["--wrapped-in", str(tmp_path), "--"]
    assert "--dangerously-skip-permissions" in argv
    env = cli.env()
    assert Path(env["GH_CONFIG_DIR"]).is_dir() and not any(
        Path(env["GH_CONFIG_DIR"]).iterdir()
    )
    assert (
        env["GIT_CONFIG_NOSYSTEM"] == "1"
        and "credential" not in Path(env["GIT_CONFIG_GLOBAL"]).read_text()
    )
    assert final.result is not None


def test_isolation_env_is_per_run(tmp_path: Path) -> None:
    a, b = isolation_env(tmp_path / "a"), isolation_env(tmp_path / "b")
    assert a["GH_CONFIG_DIR"] != b["GH_CONFIG_DIR"]
