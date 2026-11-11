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

"""The tokens each person's requests used on the service's own agents, for their
limits (``TokenLimits`` in the service settings).

A run counts toward a person when it worked for their request (``TokenLedger.whose``:
``AgentRunRecord.requested_by``, else whoever started its job, ``Job.asked_by``) and it
ran on the service's account of an agent or one of the service's model APIs
(``account`` unset). Runs on their own agent accounts or model APIs, and the service's
own work that nobody asked for, never count. The ledger keeps the last
``LONGEST`` of such runs in memory: it reads them from the store once, then only the
runs recorded since its last read.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from ..domain.clock import Clock
from ..domain.ports import Store
from ..domain.records import AgentRunRecord
from ..domain.settings import TokenLimit

WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}
"""The rolling windows a limit applies to."""
LONGEST = max(WINDOWS.values())
OVERLAP = timedelta(minutes=10)
"""How far back each read reaches before the last one, for runs recorded late."""
READ_LIMIT = 50_000
ASKERS_KEPT = 20_000
"""How many jobs' askers ``TokenLedger.whose`` keeps before it reads them again."""


def tokens(run: AgentRunRecord) -> int:
    return run.input_tokens + run.output_tokens


def short(n: int) -> str:
    """``n`` tokens, short: ``1.2M``, ``35k``."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


class TokenLedger:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self._by: dict[str, dict[str, tuple[datetime, int]]] = {}
        """Per login (lowercased), the runs that count: id -> (when, tokens)."""
        self._read_to: datetime | None = None
        self._lock = asyncio.Lock()
        self._askers: dict[str, str | None] = {}
        """Who started each job read so far (``Job.asked_by``), by job id."""

    async def whose(self, run: AgentRunRecord) -> str | None:
        """The person whose request ``run`` worked for: who asked for its task, or
        else who started its job on the dashboard or in a conversation (a run
        recorded before its playbook passed that on). ``None`` for the service's own
        work."""
        if run.requested_by:
            return run.requested_by
        if not run.job_id:
            return None
        if run.job_id not in self._askers:
            if len(self._askers) >= ASKERS_KEPT:
                self._askers.clear()
            job = await self.store.get_job(run.job_id)
            self._askers[run.job_id] = job.asked_by if job else None
        return self._askers[run.job_id]

    async def _refresh(self) -> None:
        async with self._lock:
            now = self.clock.now()
            since = (self._read_to - OVERLAP) if self._read_to else now - LONGEST
            for run in await self.store.list_agent_runs(READ_LIMIT, since=since):
                if run.account is not None:
                    continue
                who = await self.whose(run)
                if who:
                    self._by.setdefault(who.lower(), {})[run.id] = (
                        run.created_at,
                        tokens(run),
                    )
            self._read_to = now
            oldest = now - LONGEST
            for login, runs in list(self._by.items()):
                kept = {k: v for k, v in runs.items() if v[0] >= oldest}
                if kept:
                    self._by[login] = kept
                else:
                    del self._by[login]

    async def used(self, login: str) -> dict[str, int]:
        """The tokens ``login``'s requests used on the service's agents in each
        window, by window name."""
        await self._refresh()
        now = self.clock.now()
        runs = list(self._by.get(login.lower(), {}).values())
        return {
            window: sum(n for at, n in runs if at >= now - span)
            for window, span in WINDOWS.items()
        }

    async def over(self, login: str, limit: TokenLimit) -> str | None:
        """Why ``login`` may not start another run on the service's agents under
        ``limit``, or ``None`` when they may."""
        windows = limit.windows()
        if not windows:
            return None
        used = await self.used(login)
        for window, cap in windows:
            if used[window] >= cap:
                return (
                    f"token limit: {login} used {short(used[window])} of "
                    f"{short(cap)} tokens in the last {window} on the service's "
                    "agents; their own agent accounts still run"
                )
        return None
