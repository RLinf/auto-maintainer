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
