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

"""AgentRouter: fallback, breakers, budgets, structured output and repair."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from auto_maintainer.agents.adapters.generic import GenericRuntime
from auto_maintainer.agents.registry import Budget
from auto_maintainer.agents.router import AgentRouter
from auto_maintainer.agents.scripted import ScriptedAgentRuntime
from auto_maintainer.domain.agents import AgentResult, AgentTask, Capabilities, TaskKind
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.records import AgentRunRecord


class Verdict(BaseModel):
    ok: bool
    note: str = ""


def _task(**kw: object) -> AgentTask:
    data: dict[str, object] = {
        "kind": TaskKind.REVIEW,
        "prompt": "Judge it.",
        "workdir": Path("/tmp"),
        "repo": "o/r",
    }
    data.update(kw)
    return AgentTask.model_validate(data)


def _fail(kind: str) -> AgentResult:
    return AgentResult(runtime="x", ok=False, error=f"{kind} failure", error_kind=kind)  # type: ignore[arg-type]


def _ok(
    text: str = "", structured: dict | None = None, session: str | None = None
) -> AgentResult:
    return AgentResult(
        runtime="x", ok=True, text=text, structured=structured, session=session
    )


def _router(
    *runtimes: ScriptedAgentRuntime, **kw: object
) -> tuple[AgentRouter, list[AgentRunRecord]]:
    records: list[AgentRunRecord] = []

    async def record(run: AgentRunRecord) -> None:
        records.append(run)

    router = AgentRouter({r.name: r for r in runtimes}, record_run=record, **kw)  # type: ignore[arg-type]
    return router, records


async def test_falls_back_and_records_every_attempt() -> None:
    a = ScriptedAgentRuntime("a", [_fail("auth")])
    b = ScriptedAgentRuntime("b", [{"ok": True}])
    router, records = _router(a, b)
    result = await router.run(_task(), Route(primary="a", fallback=["b"]), Verdict)
    assert (
        result.ok
        and result.runtime == "b"
        and result.structured == {"ok": True, "note": ""}
    )
    assert [r.runtime for r in records] == ["a", "b"] and [r.ok for r in records] == [
        False,
        True,
    ]
    assert records[0].repo == "o/r" and records[0].task_kind == "review"


async def test_skips_a_cli_that_is_not_installed_without_opening_its_breaker(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "bin" / "agent-cli"
    missing = GenericRuntime(
        "missing", command=[str(cli), "{prompt}"], runs_dir=tmp_path / "runs"
    )
    b = ScriptedAgentRuntime("b", default={"ok": True})
    router, records = _router(missing, b)  # type: ignore[arg-type]
    route = Route(primary="missing", fallback=["b"])
    assert (await router.run(_task(), route, Verdict)).runtime == "b"
    assert [r.runtime for r in records] == ["b"], "no attempt on a missing CLI"
    assert "missing" not in router.breakers

    alone = await router.run(_task(), Route(primary="missing"), Verdict)
    assert not alone.ok and "missing: not installed" in (alone.error or "")

    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '{\"ok\": true}'\n", encoding="utf-8")
    cli.chmod(0o755)
    assert (await router.run(_task(), route, Verdict)).runtime == "missing"


async def test_model_applies_to_primary_only_effort_and_timeout_to_all() -> None:
    a = ScriptedAgentRuntime("a", [_fail("crash")])
    b = ScriptedAgentRuntime("b", [{"ok": True}])
    router, _ = _router(a, b)
    await router.run(
        _task(),
        Route(
            primary="a", fallback=["b"], model="big", effort="high", timeout_seconds=60
        ),
        Verdict,
    )
    assert a.tasks[0].model == "big" and b.tasks[0].model is None
    assert a.tasks[0].effort == b.tasks[0].effort == "high"
    assert b.tasks[0].timeout_seconds == 60


async def test_breaker_opens_on_quota_and_closes_after_cooldown() -> None:
    clock = FixedClock()
    a = ScriptedAgentRuntime("a", [_fail("quota")], default={"ok": True})
    b = ScriptedAgentRuntime("b", default={"ok": True})
    router, _ = _router(a, b, clock=clock, cooldown_seconds=600)
    route = Route(primary="a", fallback=["b"])
    assert (await router.run(_task(), route, Verdict)).runtime == "b"
    assert (await router.run(_task(), route, Verdict)).runtime == "b"
    assert len(a.tasks) == 1
    clock.advance(seconds=601)
    assert (await router.run(_task(), route, Verdict)).runtime == "a"


async def test_repair_resumes_the_session_when_supported() -> None:
    a = ScriptedAgentRuntime(
        "a",
        [_ok(text="not json", session="s-1"), _ok(text='```json\n{"ok": false}\n```')],
    )
    router, records = _router(a)
    result = await router.run(_task(), Route(primary="a"), Verdict)
    assert result.ok and result.structured == {"ok": False, "note": ""}
    repair = a.tasks[1]
    assert repair.resume_session == "s-1" and "could not be used" in repair.prompt
    assert repair.id.endswith("-repair") and len(records) == 2


async def test_repair_starts_fresh_without_sessions() -> None:
    caps = Capabilities(schema_flag=False, sessions=False)
    a = ScriptedAgentRuntime(
        "a", [_ok(text='{"ok": "maybe"}'), _ok(text='{"ok": true}')], capabilities=caps
    )
    router, _ = _router(a)
    result = await router.run(_task(), Route(primary="a"), Verdict)
    assert result.ok
    first, second = a.tasks
    assert (
        "## Output format" in first.prompt
    )  # no schema flag: instructions in the prompt
    assert second.resume_session is None and second.prompt.startswith("Judge it.")
    assert "could not be used" in second.prompt


async def test_two_invalid_answers_fall_back_and_open_the_breaker() -> None:
    clock = FixedClock()
    a = ScriptedAgentRuntime(
        "a", [_ok(text="nope"), _ok(text="still nope")], default={"ok": True}
    )
    b = ScriptedAgentRuntime("b", default={"ok": True})
    router, _ = _router(a, b, clock=clock)
    route = Route(primary="a", fallback=["b"])
    result = await router.run(_task(), route, Verdict)
    assert result.ok and result.runtime == "b"
    await router.run(_task(), route, Verdict)
    assert len(a.tasks) == 2  # skipped while the breaker is open
    other = await router.run(_task(kind=TaskKind.TRIAGE), route, Verdict)
    assert other.runtime == "a", "invalid answers pause only that kind of task"


async def test_schema_flag_runtimes_get_the_schema_not_instructions() -> None:
    a = ScriptedAgentRuntime("a", [{"ok": True}])
    router, _ = _router(a)
    await router.run(_task(), Route(primary="a"), Verdict)
    assert a.tasks[0].output_schema == Verdict.model_json_schema()
    assert "## Output format" not in a.tasks[0].prompt


async def test_budget_skips_a_runtime() -> None:
    async def usage(name: str) -> tuple[int, float]:
        return (5, 1.0) if name == "a" else (0, 0.0)

    a = ScriptedAgentRuntime("a", default={"ok": True})
    b = ScriptedAgentRuntime("b", default={"ok": True})
    router, _ = _router(a, b, budgets={"a": Budget(daily_runs=5)}, usage_today=usage)
    result = await router.run(_task(), Route(primary="a", fallback=["b"]), Verdict)
    assert result.runtime == "b" and not a.tasks


async def test_nothing_usable_explains_why() -> None:
    a = ScriptedAgentRuntime("a")
    router, _ = _router(a, disabled=["a"])
    result = await router.run(_task(), Route(primary="a", fallback=["ghost"]), Verdict)
    assert (
        not result.ok
        and "a: disabled" in (result.error or "")
        and "ghost: not configured" in (result.error or "")
    )


async def test_without_a_model_the_result_passes_through() -> None:
    a = ScriptedAgentRuntime("a", [_ok(text="plain answer")])
    router, _ = _router(a)
    result = await router.run(_task(), Route(primary="a"))
    assert result.ok and result.text == "plain answer" and result.structured is None


async def test_adapter_exceptions_become_crash_results() -> None:
    def boom(task: AgentTask) -> AgentResult:
        raise RuntimeError("adapter bug")

    a = ScriptedAgentRuntime("a", [boom])
    router, records = _router(a)
    result = await router.run(_task(), Route(primary="a"), Verdict)
    assert not result.ok and result.error_kind == "crash" and records[0].ok is False


async def test_health_reports_breakers() -> None:
    a = ScriptedAgentRuntime("a", [_fail("timeout")])
    router, _ = _router(a)
    await router.run(_task(), Route(primary="a"), Verdict)
    [health] = await router.health()
    assert health.available and "circuit open" in health.detail


@pytest.mark.parametrize("kind", ["auth", "quota", "crash", "timeout"])
async def test_breaking_failures_open_immediately(kind: str) -> None:
    a = ScriptedAgentRuntime("a", [_fail(kind)], default={"ok": True})
    router, _ = _router(a)
    await router.run(_task(), Route(primary="a"), Verdict)
    second = await router.run(_task(), Route(primary="a"), Verdict)
    assert not second.ok and "circuit open" in (second.error or "")
