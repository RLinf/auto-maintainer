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

"""The agent router: routes, fallback, circuit breakers, budgets, structured output.

A route names a primary runtime and fallbacks. The router tries them in order and
skips a runtime that is unknown, disabled, over its daily budget, or whose circuit
breaker is open. A breaker opens after an ``auth``, ``quota``, ``crash``, or
``timeout`` failure, or after two invalid structured answers in a row, and closes
after the cool-down. ``route.model`` applies to the primary runtime only, since
model names differ between CLIs; ``route.effort`` and ``route.timeout_seconds``
apply to every attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel

from ..domain.agents import AgentEventType, AgentResult, AgentTask, RuntimeHealth
from ..domain.clock import Clock, SystemClock
from ..domain.ids import new_id
from ..domain.ports import AgentQuestions, AgentRuntime
from ..domain.profile import Route
from ..domain.records import AgentRunRecord
from ..domain.settings import Budget
from .structured import extract_json, repair_prompt, schema_instructions, validate

UsageToday = Callable[[str], Awaitable[tuple[int, float]]]
"""Runs and US dollars a runtime has used today."""
RecordRun = Callable[[AgentRunRecord], Awaitable[None]]

_BREAKING_KINDS = frozenset({"auth", "quota", "crash", "timeout"})


@dataclass
class BreakerState:
    open_until: datetime | None = None
    reason: str = ""
    invalid_streak: int = 0


def effective_permissions(route: Route, runtime: AgentRuntime, task: AgentTask) -> str:
    """The route's mode, except that ``bypass`` falls back to ``auto`` unless the
    operator set ``allow_bypass`` on this runtime, the CLI has a bypass mode, and the
    input is trusted or the route sets ``bypass_untrusted``."""
    if route.permissions != "bypass":
        return route.permissions
    if not (getattr(runtime, "allow_bypass", False) and runtime.capabilities().bypass):
        return "auto"
    if task.untrusted_input and not route.bypass_untrusted:
        return "auto"
    return "bypass"


class AgentRouter:
    """Implements ``AgentRunner`` over a set of runtimes.

    Args:
        runtimes: Runtimes by name.
        clock: Time source for breaker cool-downs.
        budgets: Daily limits by runtime name.
        usage_today: Returns ``(runs, usd)`` a runtime used today; required for budgets.
        record_run: Called with a record of every attempt.
        cooldown_seconds: How long an open breaker stays open.
        disabled: Runtime names to skip.
    """

    def __init__(
        self,
        runtimes: Mapping[str, AgentRuntime],
        *,
        clock: Clock | None = None,
        budgets: Mapping[str, Budget] | None = None,
        usage_today: UsageToday | None = None,
        record_run: RecordRun | None = None,
        cooldown_seconds: float = 1800,
        disabled: Iterable[str] = (),
    ) -> None:
        self.runtimes = dict(runtimes)
        self.clock = clock or SystemClock()
        self.budgets = dict(budgets or {})
        self.usage_today = usage_today
        self.record_run = record_run
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.disabled = set(disabled)
        self.breakers: dict[str, BreakerState] = {}
        self.questions: AgentQuestions | None = None

    def bind_questions(self, questions: AgentQuestions) -> None:
        """Send every runtime's permission prompts and questions to ``questions``."""
        self.questions = questions
        for runtime in self.runtimes.values():
            if hasattr(runtime, "questions"):
                runtime.questions = questions  # type: ignore[attr-defined]

    def reconfigure(
        self,
        runtimes: Mapping[str, AgentRuntime],
        budgets: Mapping[str, Budget] | None = None,
    ) -> None:
        """Run on these runtimes from now on (new settings). Runs in progress finish on
        the runtime they started with; breakers of runtimes that changed are reset."""
        changed = {n for n in runtimes if self.runtimes.get(n) is not runtimes[n]}
        self.runtimes = dict(runtimes)
        self.budgets = dict(budgets or {})
        for name in changed | (set(self.breakers) - set(runtimes)):
            self.breakers.pop(name, None)
        if self.questions is not None:
            self.bind_questions(self.questions)

    # -- AgentRunner -----------------------------------------------------------------

    async def run(
        self, task: AgentTask, route: Route, output_model: type[BaseModel] | None = None
    ) -> AgentResult:
        """Run ``task`` on the first usable runtime of ``route``.

        Returns:
            The first ok result, with ``structured`` validated against
            ``output_model`` when one is given, or the last failure. When no runtime
            could even be tried, a failed result explains why each was skipped.
        """
        schema = (
            output_model.model_json_schema()
            if output_model is not None
            else task.output_schema
        )
        names = list(dict.fromkeys([route.primary, *route.fallback]))
        skipped: list[str] = []
        last: AgentResult | None = None
        for index, name in enumerate(names):
            reason = await self._unusable(name)
            if reason:
                skipped.append(f"{name}: {reason}")
                continue
            runtime = self.runtimes[name]
            attempt = task.model_copy(
                update={
                    "model": (route.model or task.model) if index == 0 else None,
                    "effort": route.effort or task.effort,
                    "timeout_seconds": route.timeout_seconds,
                    "question_timeout_seconds": route.question_timeout_seconds,
                    "permissions": effective_permissions(route, runtime, task),
                }
            )
            result = await self._run_structured(runtime, attempt, schema, output_model)
            if result.ok:
                return result
            last = result
        if last is not None:
            return last
        return AgentResult(
            runtime=route.primary,
            ok=False,
            error="no runtime available: " + "; ".join(skipped),
            error_kind="other",
        )

    async def health(self) -> list[RuntimeHealth]:
        """Probe every runtime and report its breaker state."""
        names = list(self.runtimes)
        probes = await asyncio.gather(
            *(self.runtimes[n].probe() for n in names), return_exceptions=True
        )
        out = []
        for name, probe in zip(names, probes, strict=True):
            if isinstance(probe, BaseException):
                probe = RuntimeHealth(
                    name=name, available=False, detail=f"probe failed: {probe!r}"
                )
            notes = []
            if name in self.disabled:
                notes.append("disabled")
            breaker = self.breakers.get(name)
            if breaker and breaker.open_until and breaker.open_until > self.clock.now():
                notes.append(
                    f"circuit open until {breaker.open_until.isoformat()} ({breaker.reason})"
                )
            if notes:
                probe = probe.model_copy(
                    update={"detail": "; ".join([probe.detail, *notes]).strip("; ")}
                )
            out.append(probe)
        return out

    # -- internals -------------------------------------------------------------------

    async def _unusable(self, name: str) -> str | None:
        if name not in self.runtimes:
            return "not configured"
        if name in self.disabled:
            return "disabled"
        breaker = self.breakers.get(name)
        if breaker and breaker.open_until:
            if breaker.open_until > self.clock.now():
                return f"circuit open ({breaker.reason})"
            breaker.open_until = None
            breaker.invalid_streak = 0
        budget = self.budgets.get(name)
        if (
            budget
            and (budget.daily_runs is not None or budget.daily_usd is not None)
            and self.usage_today
        ):
            runs, usd = await self.usage_today(name)
            if budget.daily_runs is not None and runs >= budget.daily_runs:
                return f"daily run budget of {budget.daily_runs} used"
            if budget.daily_usd is not None and usd >= budget.daily_usd:
                return f"daily budget of ${budget.daily_usd:.2f} used"
        return None

    async def _run_structured(
        self,
        runtime: AgentRuntime,
        task: AgentTask,
        schema: dict | None,
        output_model: type[BaseModel] | None,
    ) -> AgentResult:
        caps = runtime.capabilities()
        if schema is not None and not caps.schema_flag:
            task = task.model_copy(
                update={"prompt": task.prompt + schema_instructions(schema)}
            )
        task = task.model_copy(update={"output_schema": schema})
        result = await self._attempt(runtime, task)
        if not result.ok and result.error_kind != "invalid_output":
            self._failed(runtime.name, result)
            return result
        if output_model is None:
            if result.ok:
                self._succeeded(runtime.name)
            else:
                self._failed(runtime.name, result)
            return result

        checked, error = self._check(result, output_model)
        if checked is not None:
            self._succeeded(runtime.name)
            return checked

        # One repair turn: resume the session when the runtime can, else start fresh.
        assert schema is not None
        fix = repair_prompt(error or "invalid output", result.text, schema)
        if caps.sessions and result.session:
            repair = task.model_copy(
                update={"prompt": fix, "resume_session": result.session}
            )
        else:
            repair = task.model_copy(
                update={"prompt": f"{task.prompt}\n\n{fix}", "resume_session": None}
            )
        repair = repair.model_copy(update={"id": f"{task.id}-repair"})
        second = await self._attempt(runtime, repair)
        if not second.ok and second.error_kind != "invalid_output":
            self._failed(runtime.name, second)
            return second
        checked, error = self._check(second, output_model)
        if checked is not None:
            self._succeeded(runtime.name)
            return checked
        failure = second.model_copy(
            update={
                "ok": False,
                "error": error or "invalid output",
                "error_kind": "invalid_output",
            }
        )
        self._failed(runtime.name, failure, streak=2)
        return failure

    def _check(
        self, result: AgentResult, model: type[BaseModel]
    ) -> tuple[AgentResult | None, str | None]:
        data = (
            result.structured
            if result.structured is not None
            else extract_json(result.text)
        )
        obj, error = validate(data, model)
        if obj is None:
            return None, error
        return result.model_copy(
            update={
                "ok": True,
                "structured": obj.model_dump(mode="json"),
                "error": None,
                "error_kind": None,
            }
        ), None

    async def _attempt(self, runtime: AgentRuntime, task: AgentTask) -> AgentResult:
        result: AgentResult | None = None
        try:
            async for event in runtime.run(task):
                if event.type == AgentEventType.FINAL and event.result is not None:
                    result = event.result
        except Exception as exc:  # an adapter bug must not take the playbook down
            result = AgentResult(
                runtime=runtime.name,
                ok=False,
                error=f"runtime raised {exc!r}",
                error_kind="crash",
            )
        if result is None:
            result = AgentResult(
                runtime=runtime.name,
                ok=False,
                error="runtime ended without a result",
                error_kind="crash",
            )
        if self.record_run is not None:
            await self.record_run(
                AgentRunRecord(
                    id=new_id("run"),
                    job_id=task.job_id,
                    repo=task.repo,
                    runtime=runtime.name,
                    task_kind=str(task.kind),
                    ok=result.ok,
                    permissions=task.permissions,
                    error=result.error,
                    session=result.session,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cost_usd=result.usage.cost_usd,
                    transcript_path=result.transcript_path,
                    duration_seconds=result.duration_seconds,
                    created_at=self.clock.now(),
                )
            )
        return result

    def _succeeded(self, name: str) -> None:
        breaker = self.breakers.setdefault(name, BreakerState())
        breaker.invalid_streak = 0

    def _failed(self, name: str, result: AgentResult, streak: int = 1) -> None:
        breaker = self.breakers.setdefault(name, BreakerState())
        if result.error_kind in _BREAKING_KINDS:
            breaker.open_until = self.clock.now() + self.cooldown
            breaker.reason = str(result.error_kind)
            return
        if result.error_kind == "invalid_output":
            breaker.invalid_streak += streak
            if breaker.invalid_streak >= 2:
                breaker.open_until = self.clock.now() + self.cooldown
                breaker.reason = "invalid output twice in a row"
