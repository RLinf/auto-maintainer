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

"""A runtime that replays scripted results, for tests and replay evals."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from ..domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    Capabilities,
    RuntimeHealth,
)

ScriptItem = (
    AgentResult
    | list[AgentEvent]
    | dict[str, Any]
    | Callable[[AgentTask], "ScriptItem"]
)
"""One run's outcome: a result, a full event list ending in ``final``, a structured
answer (an ok result carrying it), or a callable that builds one from the task."""


class ScriptedAgentRuntime:
    """Replays ``script`` one item per run; every task it receives is kept in ``tasks``.

    Args:
        name: The runtime name.
        script: Outcomes in the order runs should produce them. When the script is
            exhausted, ``default`` is used, or the run fails.
        capabilities: What the runtime claims to support.
        default: An outcome for runs past the end of the script.
    """

    def __init__(
        self,
        name: str,
        script: Iterable[ScriptItem] = (),
        *,
        capabilities: Capabilities | None = None,
        default: ScriptItem | None = None,
    ) -> None:
        self.name = name
        self.script: deque[ScriptItem] = deque(script)
        self.default = default
        self.tasks: list[AgentTask] = []
        self._caps = capabilities or Capabilities(
            schema_flag=True, sessions=True, read_only_mode=True
        )

    def capabilities(self) -> Capabilities:
        return self._caps

    def push(self, *items: ScriptItem) -> None:
        """Append outcomes to the script."""
        self.script.extend(items)

    async def probe(self) -> RuntimeHealth:
        return RuntimeHealth(
            name=self.name, available=True, version="scripted", authenticated=True
        )

    async def run(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        self.tasks.append(task)
        if self.script:
            item: ScriptItem | None = self.script.popleft()
        else:
            item = self.default
        while callable(item):
            item = item(task)
        for event in self._events(item):
            yield event

    def _events(self, item: ScriptItem | None) -> list[AgentEvent]:
        if item is None:
            result = AgentResult(
                runtime=self.name,
                ok=False,
                error="script exhausted",
                error_kind="other",
            )
            return [AgentEvent(type=AgentEventType.FINAL, result=result)]
        if isinstance(item, AgentResult):
            result = item.model_copy(update={"runtime": self.name})
            return [
                AgentEvent(type=AgentEventType.FINAL, text=result.text, result=result)
            ]
        if isinstance(item, dict):
            result = AgentResult(
                runtime=self.name, ok=True, structured=item, text=str(item)
            )
            return [
                AgentEvent(type=AgentEventType.FINAL, text=result.text, result=result)
            ]
        events = list(item)  # type: ignore[arg-type]
        if (
            not events
            or events[-1].type != AgentEventType.FINAL
            or events[-1].result is None
        ):
            raise ValueError(
                "a scripted event list must end with a final event carrying a result"
            )
        return events
