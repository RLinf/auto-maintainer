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

"""Each job's agent steps, kept while it runs and saved when an attempt ends, so the
dashboard can show what an agent is doing as it does it (design 5.13).

Steps of running jobs live in memory and reach dashboards through
``EventBus.notify`` at most once a second per job; each attempt's steps are then saved
in the store's state, where the steps of the last ``KEEP_JOBS`` jobs stay readable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from typing import Any

from ..domain.agents import AgentEvent, AgentEventType, AgentTask
from ..domain.clock import Clock
from ..domain.events import Event, EventKind
from ..domain.ports import Store
from ..domain.progress import JobStep, step_from_event
from ..domain.secrets import redact_secrets

MAX_STEPS = 300
"""Steps kept per job; the first ones (how it started) and the newest are kept."""
KEEP_JOBS = 500
"""Jobs whose steps stay in the store; older ones are cleared."""
NOTIFY_SECONDS = 1.0
_INDEX = "job-steps:index"


def _key(job_id: str) -> str:
    return f"job-steps:{job_id}"


def _joined(text: str, more: str, limit: int = 4000) -> str:
    """``text`` followed by ``more``, redacted again (a credential can span chunks),
    keeping the last ``limit`` characters."""
    out, _ = redact_secrets(text + more)
    return out if len(out) <= limit else "…" + out[-(limit - 1) :]


class JobProgress:
    def __init__(self, store: Store, bus: Any, clock: Clock) -> None:
        self.store = store
        self.bus = bus
        self.clock = clock
        self._live: OrderedDict[str, list[JobStep]] = OrderedDict()
        self._jobs: dict[str, tuple[str | None, int | None]] = {}
        self._last_notice: dict[str, float] = {}
        self._pending: set[str] = set()
        self._lock = asyncio.Lock()

    async def observe(self, task: AgentTask, runtime: str, event: AgentEvent) -> None:
        """``ProgressObserver``: add the step ``event`` makes to the task's job."""
        if not task.job_id:
            return
        step = step_from_event(event, runtime, str(task.kind))
        if step is None:
            return
        step.at = self.clock.now()
        steps = self._live.get(task.job_id)
        if steps is None:
            steps = await self._saved(task.job_id)
            self._live[task.job_id] = steps
            while len(self._live) > 50:
                self._live.popitem(last=False)
        self._jobs[task.job_id] = (task.repo, task.pr)
        last = steps[-1] if steps else None
        if (
            last is not None
            and step.kind in ("message", "thinking")
            and last.kind == step.kind
            and last.runtime == step.runtime
        ):
            # CLIs that stream (ACP) send a message in many chunks: one step per message.
            last.text = _joined(last.text, step.text)
        else:
            step.text = _joined("", step.text)
            steps.append(step)
        if len(steps) > MAX_STEPS:
            del steps[10 : len(steps) - (MAX_STEPS - 10)]
        if event.type == AgentEventType.FINAL:
            await self._save(task.job_id, steps)
            self.changed(task.job_id, task.repo, task.pr)
        else:
            self._throttled(task.job_id)

    async def steps(self, job_id: str) -> list[JobStep]:
        """The job's steps so far, oldest first."""
        live = self._live.get(job_id)
        return list(live) if live is not None else await self._saved(job_id)

    def changed(self, job_id: str, repo: str | None, number: int | None) -> None:
        """Tell dashboards that the job changed (its steps or its status)."""
        loop = asyncio.get_running_loop()
        self._last_notice[job_id] = loop.time()
        self._pending.discard(job_id)
        notify = getattr(self.bus, "notify", None)
        if notify is None:
            return
        notify(
            Event(
                kind=EventKind.JOB_PROGRESS,
                repo=repo,
                number=number,
                payload={"job": job_id},
            )
        )

    def _throttled(self, job_id: str) -> None:
        loop = asyncio.get_running_loop()
        wait = self._last_notice.get(job_id, 0) + NOTIFY_SECONDS - loop.time()
        repo, number = self._jobs.get(job_id, (None, None))
        if wait <= 0:
            self.changed(job_id, repo, number)
        elif job_id not in self._pending:
            self._pending.add(job_id)
            loop.call_later(wait, self._late, job_id)

    def _late(self, job_id: str) -> None:
        if job_id in self._pending:
            repo, number = self._jobs.get(job_id, (None, None))
            self.changed(job_id, repo, number)

    async def _saved(self, job_id: str) -> list[JobStep]:
        raw = await self.store.get_state(_key(job_id))
        if not raw:
            return []
        out = []
        for item in raw:
            with contextlib.suppress(ValueError):
                out.append(JobStep.model_validate(item))
        return out

    async def _save(self, job_id: str, steps: list[JobStep]) -> None:
        async with self._lock:
            await self.store.set_state(
                _key(job_id), [s.model_dump(mode="json") for s in steps]
            )
            index = [
                j for j in (await self.store.get_state(_INDEX) or []) if j != job_id
            ]
            index.append(job_id)
            for old in index[:-KEEP_JOBS]:
                await self.store.set_state(_key(old), None)
            await self.store.set_state(_INDEX, index[-KEEP_JOBS:])
