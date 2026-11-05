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

"""ExecRuntime: process lifecycle, timeouts, failure classification, transcripts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from auto_maintainer.agents.adapters.generic import GenericRuntime
from auto_maintainer.agents.exec import classify_failure
from auto_maintainer.domain.agents import (
    AgentEventType,
    AgentResult,
    AgentTask,
    TaskKind,
)


def _task(tmp_path: Path, **kw: object) -> AgentTask:
    data: dict[str, object] = {
        "kind": TaskKind.REVIEW,
        "prompt": "go",
        "workdir": tmp_path,
        "timeout_seconds": 30,
    }
    data.update(kw)
    return AgentTask.model_validate(data)


async def _result(rt: GenericRuntime, task: AgentTask) -> AgentResult:
    events = [e async for e in rt.run(task)]
    assert events[-1].type == AgentEventType.FINAL and events[-1].result is not None
    return events[-1].result


def _rt(cli_path: Path, tmp_path: Path, **kw: object) -> GenericRuntime:
    return GenericRuntime(
        "fake",
        command=[str(cli_path)],
        runs_dir=tmp_path / "runs",
        kill_grace_seconds=0.5,
        **kw,
    )


async def test_timeout_kills_the_process_group(tmp_path: Path, fake_cli) -> None:
    cli = fake_cli(stdout="started\n", sleep=30, read_stdin=False)
    start = time.monotonic()
    result = await _result(_rt(cli.path, tmp_path), _task(tmp_path, timeout_seconds=1))
    assert not result.ok and result.error_kind == "timeout"
    assert time.monotonic() - start < 10


async def test_timeout_escalates_to_sigkill(tmp_path: Path, fake_cli) -> None:
    cli = fake_cli(sleep=30, ignore_term=True, read_stdin=False)
    start = time.monotonic()
    result = await _result(_rt(cli.path, tmp_path), _task(tmp_path, timeout_seconds=1))
    assert result.error_kind == "timeout" and time.monotonic() - start < 10


@pytest.mark.parametrize(
    ("stderr", "exit_code", "kind"),
    [
        ("Traceback (most recent call last):\n  File x\nKeyError: 'a'\n", 1, "crash"),
        ("Error: not logged in. Please run /login\n", 1, "auth"),
        ("429 Too Many Requests: rate limit exceeded\n", 1, "quota"),
        ("something else went wrong\n", 2, "other"),
    ],
)
async def test_failures_are_classified(
    tmp_path: Path, fake_cli, stderr: str, exit_code: int, kind: str
) -> None:
    cli = fake_cli(stderr=stderr, exit=exit_code, read_stdin=False)
    result = await _result(_rt(cli.path, tmp_path), _task(tmp_path))
    assert not result.ok and result.error_kind == kind
    assert f"code {exit_code}" in (result.error or "")


def test_signal_exit_is_a_crash() -> None:
    assert classify_failure(-11, "") == "crash"


async def test_missing_binary_is_a_crash(tmp_path: Path) -> None:
    rt = GenericRuntime(
        "nope", command=["/nonexistent/agent-cli"], runs_dir=tmp_path / "runs"
    )
    result = await _result(rt, _task(tmp_path))
    assert (
        not result.ok
        and result.error_kind == "crash"
        and "not found" in (result.error or "")
    )


async def test_schema_task_without_json_is_invalid_output(
    tmp_path: Path, fake_cli
) -> None:
    cli = fake_cli(stdout="I could not decide.\n", read_stdin=False)
    task = _task(tmp_path, output_schema={"type": "object"})
    result = await _result(_rt(cli.path, tmp_path), task)
    assert not result.ok and result.error_kind == "invalid_output"


async def test_transcript_and_files_are_scrubbed(tmp_path: Path, fake_cli) -> None:
    secret = "ghp_" + "a" * 36
    cli = fake_cli(
        stdout=json.dumps({"msg": f"using {secret}"}) + f"\nplain {secret}\n",
        stderr=f"token={secret}\n",
        read_stdin=False,
    )
    task = _task(tmp_path)
    result = await _result(_rt(cli.path, tmp_path), task)
    run_dir = tmp_path / "runs" / task.id
    for name in ("transcript.jsonl", "stderr.txt", "result.json"):
        assert secret not in (run_dir / name).read_text(), name
    assert (run_dir / "prompt.md").read_text() == "go"
    assert result.transcript_path == str(run_dir / "transcript.jsonl")


async def test_process_gets_only_the_sandbox_env(
    tmp_path: Path, fake_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "b" * 36)
    monkeypatch.setenv("AM_SESSION_SECRET", "nope")
    cli = fake_cli(read_stdin=False)
    task = _task(tmp_path, env={"GH_TOKEN": "scoped"})
    await _result(_rt(cli.path, tmp_path), task)
    env = cli.env()
    assert "GITHUB_TOKEN" not in env and "AM_SESSION_SECRET" not in env
    assert env["GH_TOKEN"] == "scoped" and "PATH" in env


async def test_workdir_is_the_cwd_and_prompt_goes_to_stdin(
    tmp_path: Path, fake_cli
) -> None:
    cli = fake_cli()
    work = tmp_path / "work"
    work.mkdir()
    result = await _result(_rt(cli.path, tmp_path), _task(work, prompt="hello agent"))
    assert result.ok and cli.stdin() == "hello agent"
    assert (cli.record_dir / "cwd.txt").read_text() == str(work)
