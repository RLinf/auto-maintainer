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


async def test_a_quick_task_stays_quick_whatever_its_route_says() -> None:
    a = ScriptedAgentRuntime("a", [{"ok": True}])
    a.effort = "high"  # type: ignore[attr-defined]
    router, _ = _router(a)
    route = Route(primary="a", effort="high", timeout_seconds=3600)
    await router.run(_task(budget="quick"), route, Verdict)
    assert (a.tasks[0].effort, a.tasks[0].timeout_seconds) == ("low", 300)


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
    a = ScriptedAgentRuntime("a", [_fail("crash")])
    router, _ = _router(a)
    await router.run(_task(), Route(primary="a"), Verdict)
    [health] = await router.health()
    assert health.available and "circuit open" in health.detail


@pytest.mark.parametrize("kind", ["auth", "quota", "crash"])
async def test_breaking_failures_open_immediately(kind: str) -> None:
    a = ScriptedAgentRuntime("a", [_fail(kind)], default={"ok": True})
    router, _ = _router(a)
    await router.run(_task(), Route(primary="a"), Verdict)
    second = await router.run(_task(), Route(primary="a"), Verdict)
    assert not second.ok and "circuit open" in (second.error or "")


async def test_a_timeout_pauses_only_its_kind_of_task() -> None:
    a = ScriptedAgentRuntime("a", [_fail("timeout")], default={"ok": True})
    router, _ = _router(a)
    long = _task(kind=TaskKind.REPRODUCE)
    await router.run(long, Route(primary="a"), Verdict)
    again = await router.run(long, Route(primary="a"), Verdict)
    assert not again.ok and "circuit open (timeout)" in (again.error or "")
    review = await router.run(_task(), Route(primary="a"), Verdict)
    assert review.ok, "a reproduction that ran out of time leaves reviews running"


async def test_a_turn_that_ends_early_is_asked_to_finish_the_work() -> None:
    a = ScriptedAgentRuntime(
        "a",
        [_ok("I started the install and will continue once it ends.", session="s1")],
        default={"ok": True},
    )
    router, _ = _router(a)
    result = await router.run(_task(), Route(primary="a"), Verdict)
    assert result.ok
    repair = a.tasks[-1]
    assert repair.resume_session == "s1"
    assert (
        "finish it now" in repair.prompt and "wait for every command" in repair.prompt
    )


async def test_an_agents_own_effort_wins_over_the_routes() -> None:
    a = ScriptedAgentRuntime("a", default={"ok": True})
    a.effort = "high"  # type: ignore[attr-defined]
    b = ScriptedAgentRuntime("b", [_fail("crash")], default={"ok": True})
    router, _ = _router(a, b)
    await router.run(_task(), Route(primary="a", effort="low"), Verdict)
    await router.run(_task(), Route(primary="b", fallback=["a"], effort="low"), Verdict)
    assert [t.effort for t in a.tasks] == ["high", "high"]
    assert b.tasks[0].effort == "low", (
        "an agent without its own level follows the route"
    )


async def test_a_task_in_a_session_gets_at_least_the_sessions_time() -> None:
    a = ScriptedAgentRuntime("a", default={"ok": True})
    router, _ = _router(a)
    route = Route(primary="a", timeout_seconds=1800)
    await router.run(_task(min_timeout_seconds=5400), route, Verdict)
    await router.run(_task(), route, Verdict)
    assert [t.timeout_seconds for t in a.tasks] == [5400, 1800], (
        "the route's timeout holds unless the task needs longer"
    )


async def test_every_run_may_read_the_services_own_code(tmp_path: Path) -> None:
    """Whatever the task, the agent gets the service's code read-only and is told
    where it is; a task that works in that code already gets nothing more."""
    code = tmp_path / "service-code" / "abc123"
    code.mkdir(parents=True)
    a = ScriptedAgentRuntime("a", default={"ok": True})
    router, _ = _router(a, service_code=code)
    await router.run(_task(), Route(primary="a"), Verdict)
    await router.run(_task(workdir=code), Route(primary="a"), Verdict)
    first, second = a.tasks
    assert first.reference_dirs == [code] and f"is at {code}, read-only" in first.prompt
    assert second.reference_dirs == [] and str(code) not in second.prompt


async def test_an_agent_signed_out_without_a_provider_is_passed_over() -> None:
    """An agent whose account is not signed in and that has no model provider would
    only fail, so routes and the default pass over it; with a provider it runs."""
    a = ScriptedAgentRuntime("a", [{"ok": True}])
    b = ScriptedAgentRuntime("b", [{"ok": True}])
    router, records = _router(a, b)
    signed = {"a": False, "b": None}

    async def check(name: str) -> bool | None:
        return signed[name]

    router.signed_in_check = check
    route = Route(primary="a", fallback=["b"])
    result = await router.run(_task(), route, Verdict)
    assert result.ok and result.runtime == "b"
    assert [r.runtime for r in records] == ["b"], "a never ran"
    assert router.runs_on(route) == "b" and not router.usable("a")
    assert router.ready("a"), "it is still installed"

    signed["a"] = True
    a.provider = None
    assert await router._unusable("a") is None and router.usable("a")


async def test_a_signed_out_agent_still_runs_on_its_requesters_own_account() -> None:
    from auto_maintainer.domain.agents import PersonalAgent

    a = ScriptedAgentRuntime("a", [{"ok": True}])
    a.container_sandbox = object()  # type: ignore[attr-defined]
    router, _ = _router(a)

    async def check(name: str) -> bool | None:
        return False

    class People:
        async def get(self, login: str) -> dict[str, PersonalAgent]:
            return (
                {"a": PersonalAgent(agent="a", use_own=True)}
                if login == "maria"
                else {}
            )

    router.signed_in_check = check
    router.people = People()  # type: ignore[assignment]
    assert await router._unusable("a", "review", _task(requested_by="maria")) is None
    assert await router._unusable("a", "review", _task(requested_by="walt"))


class _People:
    """maria runs ``b`` on her own account; nobody else runs any."""

    async def get(self, login: str) -> dict[str, object]:
        from auto_maintainer.domain.agents import PersonalAgent

        return (
            {"b": PersonalAgent(agent="b", use_own=True)}
            if login.lower() == "maria"
            else {}
        )

    async def providers(self, login: str) -> dict[str, object]:
        return {}

    def home(self, login: str) -> Path:
        return Path("/people") / login


def _own(name: str, script: list) -> ScriptedAgentRuntime:
    runtime = ScriptedAgentRuntime(name, script)
    runtime.container_sandbox = object()  # type: ignore[attr-defined]
    runtime.account_home = None  # type: ignore[attr-defined]
    return runtime


async def test_what_a_person_asks_for_tries_their_own_agents_first() -> None:
    """Their own agent goes first, unless they chose the agent, the task resumes a
    session, or it verifies (a second opinion from the route's agent); nobody
    else's task moves."""
    a = _own("a", [])
    b = _own("b", [])
    router, _ = _router(a, b)
    router.people = _People()  # type: ignore[assignment]
    route = Route(primary="a", fallback=[], model="a-model")
    a.default = b.default = {"ok": True}

    mine = await router.run(_task(requested_by="Maria"), route, Verdict)
    assert mine.ok and mine.runtime == "b"
    assert b.tasks[-1].model is None, "the route's model is the route's agent's"
    assert (await router.run(_task(requested_by="walt"), route, Verdict)).runtime == "a"
    assert (await router.run(_task(), route, Verdict)).runtime == "a"
    for kept in (
        _task(requested_by="maria", agent_chosen=True),
        _task(requested_by="maria", resume_session="s1"),
        _task(requested_by="maria", kind=TaskKind.VERIFY),
    ):
        assert (await router.run(kept, route, Verdict)).runtime == "a", kept
    assert await router.own_agents("maria") == ["b"]
    assert await router.own_agents("walt") == []


async def test_a_persons_own_account_failing_leaves_the_services_running() -> None:
    a = _own("a", [])
    b = _own("b", [_fail("auth")])
    b.default = a.default = {"ok": True}
    router, _ = _router(a, b)
    router.people = _People()  # type: ignore[assignment]
    route = Route(primary="b", fallback=["a"])

    first = await router.run(_task(requested_by="maria"), route, Verdict)
    assert first.ok and first.runtime == "a", "her account failed; the route went on"
    assert "b" not in router.breakers and router.breakers["b@maria"].open_until
    assert (await router.run(_task(requested_by="maria"), route, Verdict)).runtime == (
        "a"
    ), "her own breaker is open"
    assert (await router.run(_task(requested_by="walt"), route, Verdict)).runtime == (
        "b"
    ), "the service's account of b still runs"


async def test_a_person_over_their_token_limit_gets_no_service_agent() -> None:
    async def limit(login: str) -> str | None:
        return "token limit: walt used 2.0M of 2.0M tokens" if login == "walt" else None

    a = ScriptedAgentRuntime("a", default={"ok": True})
    b = ScriptedAgentRuntime("b", default={"ok": True})
    router, records = _router(a, b)
    router.person_limit = limit
    route = Route(primary="a", fallback=["b"])
    refused = await router.run(_task(requested_by="walt"), route, Verdict)
    assert not refused.ok and refused.error_kind == "limit"
    assert refused.error == "token limit: walt used 2.0M of 2.0M tokens", "said once"
    assert not a.tasks and not b.tasks and not records
    assert (await router.run(_task(requested_by="maria"), route, Verdict)).ok
    assert (await router.run(_task(), route, Verdict)).ok, "nobody's own work is free"
    assert "a" not in router.breakers or not router.breakers["a"].open_until


async def test_quick_tasks_run_on_an_agents_quick_model() -> None:
    """The service settings' ``quick_model`` takes triage, scores, and other short
    answers, unless the route names a model; other work keeps the agent's model."""
    a = ScriptedAgentRuntime("a", default={"ok": True})
    a.quick_model = "fast"  # type: ignore[attr-defined]
    router, _ = _router(a)
    route = Route(primary="a")
    for task in (
        _task(kind=TaskKind.TRIAGE),
        _task(kind=TaskKind.REVIEW),
        _task(kind=TaskKind.REVIEW, budget="quick"),
        _task(kind=TaskKind.CI_DIAGNOSE),
    ):
        await router.run(task, route)
    await router.run(_task(kind=TaskKind.TRIAGE), Route(primary="a", model="named"))
    assert [t.model for t in a.tasks] == ["fast", None, "fast", "fast", "named"]
