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

"""Contract tests against the installed agent CLIs. They cost money; run with
``uv run pytest -m live_agent tests/agents``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from auto_maintainer.agents.exec import resolve_binary
from auto_maintainer.agents.registry import DEFAULT_RUNTIMES, build_runtime
from auto_maintainer.agents.router import AgentRouter
from auto_maintainer.domain.agents import AgentTask, TaskKind
from auto_maintainer.domain.profile import Route

pytestmark = pytest.mark.live_agent

NATIVE = ["claude-code", "codex", "antigravity", "opencode", "gemini"]


class Ok(BaseModel):
    ok: bool


@pytest.mark.parametrize("name", NATIVE)
async def test_cli_returns_a_schema_answer(name: str, tmp_path: Path) -> None:
    runtime = build_runtime(name, DEFAULT_RUNTIMES[name], runs_dir=tmp_path / "runs")
    if resolve_binary(runtime.binary, runtime.search_dirs) is None:  # type: ignore[attr-defined]
        pytest.skip(f"{name} is not installed")
    health = await runtime.probe()
    assert health.available, health.detail
    work = tmp_path / "work"
    work.mkdir()
    task = AgentTask(
        kind=TaskKind.PROBE,
        prompt='Answer with {"ok": true}. Do not use any tools.',
        workdir=work,
        timeout_seconds=180,
    )
    result = await AgentRouter({name: runtime}).run(
        task, Route(primary=name, timeout_seconds=180), Ok
    )
    if not result.ok and result.error_kind in ("auth", "quota"):
        pytest.skip(f"{name} is not usable on this host: {result.error}")
    assert result.ok, result.error
    assert result.structured == {"ok": True}
