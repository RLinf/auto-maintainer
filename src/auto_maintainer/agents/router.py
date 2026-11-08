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
skips a runtime that is unknown, disabled, not installed (its ``installed()`` says
so), over its daily budget, or whose circuit breaker is open. A missing CLI opens
no breaker, so installing it makes it usable at once. A breaker opens for the whole
runtime after an ``auth``, ``quota``, ``crash``, or ``timeout`` failure, and for one
task kind on that runtime after two invalid structured answers in a row (a CLI that
cannot answer one kind of task still runs the others); both close after the
cool-down. ``route.model`` applies to the primary runtime only, since
model names differ between CLIs; ``route.effort`` (unless the agent has its own
``effort``) and ``route.timeout_seconds`` apply to every attempt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ..domain.agents import (
    QUICK_TIMEOUT_SECONDS,
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    RuntimeHealth,
)
from ..domain.clock import Clock, SystemClock
from ..domain.ids import new_id
from ..domain.ports import AgentQuestions, AgentRuntime, ProgressObserver
from ..domain.profile import Route
from ..domain.records import AgentRunRecord
from ..domain.settings import Budget
from .exec import Inbox, Told
from .sandbox import scrub
from .structured import extract_json, repair_prompt, schema_instructions, validate

UsageToday = Callable[[str], Awaitable[tuple[int, float]]]
"""Runs and US dollars a runtime has used today."""
RecordRun = Callable[[AgentRunRecord], Awaitable[None]]

log = logging.getLogger(__name__)


def _scrubbed(value: Any) -> Any:
    """``value`` with every string in it scrubbed of credentials."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    return value


_BREAKING_KINDS = frozenset({"auth", "quota", "crash", "timeout"})


@dataclass
class BreakerState:
    open_until: datetime | None = None
    reason: str = ""
    invalid_streak: int = 0


def effective_permissions(route: Route, runtime: AgentRuntime, task: AgentTask) -> str:
    """``bypass`` for a runtime that runs each task in its own container (it holds
    nothing worth taking; design 5.7). Otherwise the agent's own mode when the service
    settings give it one, else the route's; ``bypass`` falls back to ``auto`` unless it
    is allowed on this runtime (by ``allow_bypass`` or by the agent's own ``bypass``),
    the CLI has a bypass mode, and the input is trusted or the route sets
    ``bypass_untrusted``."""
    if (
        getattr(runtime, "container_sandbox", None) is not None
        and runtime.capabilities().bypass
    ):
        return "bypass"  # in its own container everything is permitted
    own = getattr(runtime, "own_permissions", None)
    mode = own or route.permissions
    if mode != "bypass":
        return mode
    allowed = own == "bypass" or getattr(runtime, "allow_bypass", False)
    if not (allowed and runtime.capabilities().bypass):
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
        self.output_breakers: dict[tuple[str, str], BreakerState] = {}
        """Breakers opened by invalid answers, per runtime and task kind."""
        self.questions: AgentQuestions | None = None
        self.progress: ProgressObserver | None = None
        self.inbox = Inbox()
        """Reviewers' messages for the agents running now, by job."""
        self._bind_inbox()

    def _bind_inbox(self) -> None:
        for runtime in self.runtimes.values():
            if hasattr(runtime, "inbox"):
                runtime.inbox = self.inbox  # type: ignore[attr-defined]

    def tell(self, job_id: str, by: str, text: str) -> bool:
        """Bring a reviewer's message to the agent running for ``job_id`` (``AgentRunner``)."""
        return self.inbox.put(job_id, Told(by=by, text=text))

    def listening(self, job_id: str) -> bool:
        """Whether an agent running for ``job_id`` takes messages (``AgentRunner``)."""
        return self.inbox.listening(job_id)

    def bind_progress(self, observer: ProgressObserver) -> None:
        """Report every attempt's events to ``observer`` (``AgentRunner``)."""
        self.progress = observer

    async def _report(self, task: AgentTask, runtime: str, event: AgentEvent) -> None:
        if self.progress is None:
            return
        cleaned = event.model_copy(
            update={
                "text": scrub(event.text),
                "data": _scrubbed(event.data),
            }
        )
        try:
            await self.progress(task, runtime, cleaned)
        except Exception:  # progress is for people watching; it never stops a run
            log.exception("progress observer failed")

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
        self._bind_inbox()
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
            reason = await self._unusable(name, str(task.kind))
            if reason:
                skipped.append(f"{name}: {reason}")
                continue
            runtime = self.runtimes[name]
            attempt = task.model_copy(
                update={
                    "model": (route.model or task.model) if index == 0 else None,
                    # An agent's own reasoning level wins over the route's, as its
                    # own permission mode does.
                    "effort": "low"
                    if task.budget == "quick"
                    else getattr(runtime, "effort", None)
                    or route.effort
                    or task.effort,
                    "timeout_seconds": min(route.timeout_seconds, QUICK_TIMEOUT_SECONDS)
                    if task.budget == "quick"
                    else route.timeout_seconds,
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

    def ready(self, name: str) -> bool:
        """Whether ``name`` is configured, enabled, and installed on this host
        (breakers and budgets aside)."""
        runtime = self.runtimes.get(name)
        if runtime is None or name in self.disabled:
            return False
        installed = getattr(runtime, "installed", None)
        return not callable(installed) or bool(installed())

    def runs_on(self, route: Route) -> str | None:
        """The agent ``route`` runs on now: its first ready runtime."""
        return next(
            (
                n
                for n in dict.fromkeys([route.primary, *route.fallback])
                if self.ready(n)
            ),
            None,
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
            for (runtime, kind), held in self.output_breakers.items():
                if (
                    runtime == name
                    and held.open_until
                    and held.open_until > self.clock.now()
                ):
                    notes.append(
                        f"{kind} paused until {held.open_until.isoformat()} ({held.reason})"
                    )
            if notes:
                probe = probe.model_copy(
                    update={"detail": "; ".join([probe.detail, *notes]).strip("; ")}
                )
            out.append(probe)
        return out

    # -- internals -------------------------------------------------------------------

    async def _unusable(self, name: str, kind: str = "") -> str | None:
        if name not in self.runtimes:
            return "not configured"
        if name in self.disabled:
            return "disabled"
        installed = getattr(self.runtimes[name], "installed", None)
        if callable(installed) and not installed():
            return "not installed on this host"
        for breaker in (
            self.breakers.get(name),
            self.output_breakers.get((name, kind)),
        ):
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
            self._failed(runtime.name, result, kind=str(task.kind))
            return result
        if output_model is None:
            if result.ok:
                self._succeeded(runtime.name, str(task.kind))
            else:
                self._failed(runtime.name, result, kind=str(task.kind))
            return result

        checked, error = self._check(result, output_model)
        if checked is not None:
            self._succeeded(runtime.name, str(task.kind))
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
            self._failed(runtime.name, second, kind=str(task.kind))
            return second
        checked, error = self._check(second, output_model)
        if checked is not None:
            self._succeeded(runtime.name, str(task.kind))
            return checked
        failure = second.model_copy(
            update={
                "ok": False,
                "error": error or "invalid output",
                "error_kind": "invalid_output",
            }
        )
        self._failed(runtime.name, failure, streak=2, kind=str(task.kind))
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
        await self._report(task, runtime.name, AgentEvent(type=AgentEventType.STARTED))
        try:
            async for event in runtime.run(task):
                if event.type == AgentEventType.FINAL and event.result is not None:
                    result = event.result
                else:
                    await self._report(task, runtime.name, event)
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
        await self._report(
            task,
            runtime.name,
            AgentEvent(
                type=AgentEventType.FINAL,
                result=result.model_copy(update={"error": scrub(result.error or "")}),
            ),
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

    def _succeeded(self, name: str, kind: str = "") -> None:
        self.output_breakers.setdefault((name, kind), BreakerState()).invalid_streak = 0

    def _failed(
        self, name: str, result: AgentResult, streak: int = 1, kind: str = ""
    ) -> None:
        if result.error_kind in _BREAKING_KINDS:
            breaker = self.breakers.setdefault(name, BreakerState())
            breaker.open_until = self.clock.now() + self.cooldown
            breaker.reason = str(result.error_kind)
            return
        if result.error_kind == "invalid_output":
            breaker = self.output_breakers.setdefault((name, kind), BreakerState())
            breaker.invalid_streak += streak
            if breaker.invalid_streak >= 2:
                breaker.open_until = self.clock.now() + self.cooldown
                breaker.reason = "invalid output twice in a row"
