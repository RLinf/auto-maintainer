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

"""Each agent CLI's settings (``domain/agent_options.py``) reach one run alone: a
flag, settings passed inline, the environment, or a file in the run's folder; an
option nobody set passes nothing, so the CLI's default stands."""

from __future__ import annotations

import json
from pathlib import Path

from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.agents.adapters.gemini import GeminiRuntime
from auto_maintainer.agents.adapters.opencode import OpenCodeRuntime
from auto_maintainer.agents.exec import RunFiles
from auto_maintainer.agents.registry import build_runtime
from auto_maintainer.domain.agent_options import (
    AGENT_OPTIONS,
    option_problems,
    set_options,
)
from auto_maintainer.domain.agents import AgentTask, TaskKind
from auto_maintainer.domain.settings import RuntimeConfig, ServiceConfig, check_settings


def _task(tmp_path: Path, **kw: object) -> AgentTask:
    return AgentTask.model_validate(
        {"kind": TaskKind.REVIEW, "prompt": "Review it.", "workdir": tmp_path, **kw}
    )


def _files(tmp_path: Path) -> RunFiles:
    return RunFiles.create(tmp_path / "runs", "run_test")


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_every_option_states_its_default_and_checks_its_values() -> None:
    for adapter, options in AGENT_OPTIONS.items():
        for o in options:
            assert o.default, f"{adapter}.{o.key} says what applies unset"
            assert not o.by_service or o.cli_default, "and the CLI's own beside ours"
            assert o.kind != "choice" or o.choices
    assert (
        option_problems("codex", {"web_search": "live", "network_access": True}) == []
    )
    assert option_problems("codex", {"web_search": "always"}) == [
        "web_search is one of disabled, cached, live"
    ]
    assert option_problems("gemini", {"compression_threshold": 2}) == [
        "compression_threshold is from 0 to 1"
    ]
    assert option_problems("pi", {"sandbox": True}) == ["pi has no option 'sandbox'"]
    assert set_options("codex", {"web_search": "", "verbosity": "low", "x": 1}) == {
        "verbosity": "low"
    }


def test_unset_options_pass_nothing(tmp_path: Path) -> None:
    files = _files(tmp_path)
    task = _task(tmp_path)
    claude = ClaudeCodeRuntime().build_invocation(task, files, "claude").argv
    assert "--settings" not in claude and "--fallback-model" not in claude
    assert ClaudeCodeRuntime().task_env(task) == {}
    codex = CodexRuntime().build_invocation(task, files, "codex").argv
    assert "-c" not in codex
    gemini = GeminiRuntime()
    gemini.build_invocation(task, files, "gemini")
    assert (
        gemini.task_env(task) == {}
        and not (files.dir / "gemini-settings.json").exists()
    )
    config = json.loads(OpenCodeRuntime().task_env(task)["OPENCODE_CONFIG_CONTENT"])
    assert set(config) == {"permission"}


def test_claude_code_takes_its_options_inline_and_from_its_environment(
    tmp_path: Path,
) -> None:
    claude = ClaudeCodeRuntime(
        options={
            "sandbox": True,
            "sandbox_weaker_nested": True,
            "sandbox_allowed_domains": ["pypi.org"],
            "thinking": False,
            "attribution": False,
            "fallback_model": "sonnet",
            "web_fetch": False,
            "bash_timeout_seconds": 300,
        }
    )
    task = _task(tmp_path)
    argv = claude.build_invocation(task, _files(tmp_path), "claude").argv
    assert json.loads(_after(argv, "--settings")) == {
        "sandbox": {
            "enabled": True,
            "enableWeakerNestedSandbox": True,
            "network": {"allowedDomains": ["pypi.org"]},
        },
        "alwaysThinkingEnabled": False,
        "attribution": {"commit": "", "pr": ""},
    }
    assert _after(argv, "--setting-sources") == "", "no settings file is read"
    assert _after(argv, "--fallback-model") == "sonnet"
    assert claude.task_env(task) == {
        "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
        "BASH_DEFAULT_TIMEOUT_MS": "300000",
    }


def test_codex_takes_its_options_as_overrides_of_one_app_server(tmp_path: Path) -> None:
    codex = CodexRuntime(
        options={
            "network_access": True,
            "web_search": "disabled",
            "reasoning_summary": "concise",
            "approvals_reviewer": "user",
        }
    )
    argv = codex.build_invocation(_task(tmp_path), _files(tmp_path), "codex").argv
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert overrides == [
        "sandbox_workspace_write.network_access=true",
        'web_search="disabled"',
        'model_reasoning_summary="concise"',
    ]
    assert codex.thread_settings(_task(tmp_path))["approvalsReviewer"] == "user"
    assert CodexRuntime().thread_settings(_task(tmp_path))["approvalsReviewer"] == (
        "auto_review"
    ), "the service's default"


def test_antigravity_sandboxes_the_tasks_its_option_names(tmp_path: Path) -> None:
    files = _files(tmp_path)
    read_only, writing = _task(tmp_path), _task(tmp_path, mode="workspace_write")
    for sandbox, ro, rw in (
        (None, True, False),
        ("always", True, True),
        ("off", False, False),
    ):
        agy = AntigravityRuntime(options={"sandbox": sandbox} if sandbox else {})
        assert ("--sandbox" in agy.build_invocation(read_only, files, "agy").argv) is ro
        assert ("--sandbox" in agy.build_invocation(writing, files, "agy").argv) is rw


def test_gemini_reads_its_options_from_a_file_of_the_run_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    gemini = GeminiRuntime(
        options={
            "checkpointing": True,
            "loop_detection": False,
            "max_session_turns": 50,
        }
    )
    files, task = _files(tmp_path), _task(tmp_path)
    gemini.build_invocation(task, files, "gemini")
    env = gemini.task_env(task)
    path = Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
    assert path.parent == files.dir, "in the run's own folder"
    assert json.loads(path.read_text()) == {
        "general": {"checkpointing": {"enabled": True}},
        "model": {"disableLoopDetection": True, "maxSessionTurns": 50},
    }
    assert not home.exists(), "no settings.json of the CLI is written"
    assert gemini.task_env(task) == {}, "and only that run reads it"


def test_opencode_takes_its_options_in_its_inline_config(tmp_path: Path) -> None:
    oc = OpenCodeRuntime(
        options={"share": "disabled", "auto_compact": False, "update_check": False}
    )
    config = json.loads(oc.task_env(_task(tmp_path))["OPENCODE_CONFIG_CONTENT"])
    assert config["share"] == "disabled"
    assert config["compaction"] == {"auto": False}
    assert config["autoupdate"] is False
    assert "permission" in config, "the service's permissions stay"


def test_the_settings_file_gives_options_to_runs_and_keeps_adapter_arguments(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(
        adapter="claude-code",
        options={"sandbox": True, "setting_sources": "user"},
    )
    runtime = build_runtime("claude-code", config, tmp_path)
    assert runtime.options == {"sandbox": True}  # type: ignore[attr-defined]
    assert runtime.setting_sources == "user"  # type: ignore[attr-defined]
    old = build_runtime(
        "antigravity", RuntimeConfig(adapter="antigravity", options={"sandbox": False})
    )
    assert old.options == {"sandbox": "off"}, (
        "an older file's true or false still reads"
    )  # type: ignore[attr-defined]
    problems = check_settings(
        ServiceConfig.model_validate(
            {"agents": {"codex": {"adapter": "codex", "options": {"web_search": "x"}}}}
        ),
        {},
    )
    assert problems == [
        "agents.codex.options: web_search is one of disabled, cached, live"
    ]
