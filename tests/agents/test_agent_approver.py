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

"""Automatic answers for ACP runtimes: what is settled without a reviewer."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_maintainer.agents.approver import auto_answer, safe_command
from auto_maintainer.domain.agents import (
    AgentQuestion,
    AgentTask,
    QuestionKind,
    TaskKind,
)


def _task(tmp_path: Path, mode: str = "read_only") -> AgentTask:
    return AgentTask(kind=TaskKind.REVIEW, prompt="", workdir=tmp_path, mode=mode)  # type: ignore[arg-type]


def _ask(tool: str, detail: str = "", paths: list[str] | None = None) -> AgentQuestion:
    return AgentQuestion(
        kind=QuestionKind.PERMISSION,
        title="x",
        tool=tool,
        detail=detail,
        paths=paths or [],
    )


@pytest.mark.parametrize(
    "command",
    [
        "git diff HEAD~1",
        "git log --oneline -5 | head -3",
        "rg -n foo src",
        "ls -la",
        "find . -name '*.py'",
        "git show abc123 --stat && git diff main...abc123",
        "git status; git log -n 5 --oneline",
        "git diff --stat || git status",
        "grep -n 'a && b; c' src/x.py",
        "which pytest ruff sphinx-build uv",
        "git branch -a && git remote -v && git log -n 5 --oneline",
        "git remote get-url origin",
        "git diff --stat 2>/dev/null || true",
        "ls -la 2>&1 | head -5",
        "echo checked",
    ],
)
def test_reading_commands_are_approved(tmp_path: Path, command: str) -> None:
    assert safe_command(command, "read_only", [tmp_path])


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > out.txt",
        "cat /etc/passwd",
        "cat ~/.config/gh/hosts.yml",
        "cat ../../secret",
        "git -c core.pager=sh log",
        "git push origin main",
        "find . -delete",
        "curl https://example.com",
        "ls; rm -rf /",
        "ls $(whoami)",
        "pytest -q",
        "FOO=1 ls",
        "git status && pytest -q",
        "git log & curl https://example.com",
        "(cd /etc && cat passwd)",
        "git log 2>/tmp/x",
        "cat x.py >> y.py",
        "git status;",
        "git status && FOO=1 ls",
        "git remote add mirror https://example.com/x.git",
        "ls -la /opt/venv 2>/dev/null",
        "echo hi > /dev/nullx",
        "echo hi >/dev/null/../../tmp/x",
    ],
)
def test_risky_commands_go_to_a_reviewer(tmp_path: Path, command: str) -> None:
    assert not safe_command(command, "read_only", [tmp_path])


def test_writing_tasks_may_test_and_commit(tmp_path: Path) -> None:
    for command in (
        "uv run pytest -q",
        "ruff check .",
        "git add -A",
        "git commit -m x",
    ):
        assert safe_command(command, "workspace_write", [tmp_path]), command
    assert not safe_command("git push", "workspace_write", [tmp_path])


def test_a_symlink_out_of_the_worktree_is_not_inside(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-secret"
    outside.write_text("token")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x").symlink_to(outside)
    assert not safe_command("cat docs/x", "read_only", [tmp_path])
    assert (
        auto_answer(_task(tmp_path), _ask("read", paths=[str(tmp_path / "docs" / "x")]))
        is None
    )


def test_edits(tmp_path: Path) -> None:
    denied = auto_answer(_task(tmp_path), _ask("edit", paths=[str(tmp_path / "a.py")]))
    assert denied is not None and not denied.allow and denied.automatic
    allowed = auto_answer(
        _task(tmp_path, "workspace_write"), _ask("edit", paths=[str(tmp_path / "a.py")])
    )
    assert allowed is not None and allowed.allow
    assert (
        auto_answer(
            _task(tmp_path, "workspace_write"), _ask("edit", paths=["/etc/hosts"])
        )
        is None
    )


def test_other_requests_need_a_reviewer(tmp_path: Path) -> None:
    assert auto_answer(_task(tmp_path), _ask("fetch", "https://x")) is None
    assert auto_answer(_task(tmp_path), _ask("other")) is None
    question = AgentQuestion(kind=QuestionKind.INPUT, title="which?")
    assert auto_answer(_task(tmp_path), question) is None


def test_commands_an_agents_settings_approve_run_without_asking(tmp_path: Path) -> None:
    import shlex

    approved = [
        shlex.split("python3 .agents/skills/docs-check/check_rst_markup.py"),
        shlex.split("python3 .agents/skills/review-pr/lint_review.py lint"),
    ]
    (tmp_path / "docs").mkdir()
    for command in [
        "python3 .agents/skills/docs-check/check_rst_markup.py docs/index.rst",
        "python3 .agents/skills/review-pr/lint_review.py lint --pr 3 review.json",
        "git status && python3 .agents/skills/docs-check/check_rst_markup.py docs",
    ]:
        assert safe_command(command, "read_only", [tmp_path], approved), command
    for command in [
        "python3 .agents/skills/docs-check/check_rst_markup.py /etc/passwd",
        "python3 .agents/skills/review-pr/lint_review.py post --pr 3 review.json",
        "python3 .agents/skills/docs-check/build_docs.py",
        "python3 .agents/skills/docs-check/check_rst_markup.py docs && pytest -q",
    ]:
        assert not safe_command(command, "read_only", [tmp_path], approved), command
    assert not safe_command(
        "python3 .agents/skills/docs-check/check_rst_markup.py docs",
        "read_only",
        [tmp_path],
    ), "nothing is approved unless the settings say so"


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ("python3", "needs the script"),
        ("python3 -c 'import os'", "needs the script"),
        ("bash -c ls", "needs the script"),
        ("uv run ruff check", "runs other code"),
        ("npm test", "runs other code"),
        ("curl https://example.com", "runs other code"),
        ("ls; rm -rf /", "shell syntax"),
        ("cat x > y", "shell syntax"),
        ("", "empty"),
    ],
)
def test_settings_refuse_commands_that_could_run_anything(
    command: str, problem: str
) -> None:
    from auto_maintainer.domain.settings import approve_command_problems

    assert any(problem in p for p in approve_command_problems(command))
    assert not approve_command_problems(
        "python3 .agents/skills/docs-check/check_rst_markup.py"
    )


def test_an_agents_approved_commands_reach_its_answers(tmp_path: Path) -> None:
    from auto_maintainer.agents.registry import build_runtime
    from auto_maintainer.domain.profile import Route
    from auto_maintainer.domain.settings import (
        RuntimeConfig,
        ServiceConfig,
        check_settings,
    )

    script = "python3 .agents/skills/docs-check/check_rst_markup.py"
    runtime = build_runtime(
        "antigravity",
        RuntimeConfig(adapter="antigravity", approve_commands=[script]),
        runs_dir=tmp_path / "runs",
    )
    answer = runtime.auto_answer(
        _task(tmp_path), _ask("execute", f"{script} README.rst")
    )
    assert answer is not None and answer.allow and answer.automatic
    other = build_runtime(
        "antigravity", RuntimeConfig(adapter="antigravity"), runs_dir=tmp_path / "runs"
    )
    assert (
        other.auto_answer(_task(tmp_path), _ask("execute", f"{script} README.rst"))
        is None
    )
    config = ServiceConfig(
        agents={"antigravity": {"approve_commands": ["python3 -m pytest"]}}
    )
    problems = check_settings(config, {"review": Route(primary="antigravity")})
    assert any("approve_commands.0" in p and "needs the script" in p for p in problems)


def test_am_read_is_a_read_of_the_dashboard_not_of_a_file(tmp_path: Path) -> None:
    assert safe_command("am-read /api/prs?mine=true", "read_only", [tmp_path])
    assert safe_command(
        "am-read /api/prs/acme/widget/42/record | jq .", "read_only", [tmp_path]
    )
    assert not safe_command("am-read /api/prs; rm -rf /", "read_only", [tmp_path])
