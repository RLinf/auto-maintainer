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

"""The engine (design 5.4): events become jobs; workers run playbooks; waits park jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from ..actuator.pipeline import ActionPipeline
from ..domain.clock import Clock
from ..domain.events import Event, EventKind
from ..domain.ports import EventSink, Store
from ..domain.records import Job, JobStatus, WaitSpec
from ..playbooks.base import Playbook, PlaybookContext, PlaybookResult
from ..profiles.service import ProfileService

log = logging.getLogger(__name__)

ContextFactory = Callable[[Job, Event], Awaitable[PlaybookContext]]

_ACTION_EVENTS = {
    EventKind.ACTION_EXECUTED,
    EventKind.ACTION_FAILED,
    EventKind.PROPOSAL_DECIDED,
}
"""Events about one action: a job waiting on action ids wakes only for those."""


class Engine:
    def __init__(
        self,
        store: Store,
        playbooks: list[Playbook],
        context: ContextFactory,
        pipeline: ActionPipeline,
        profiles: ProfileService,
        bus: EventSink,
        clock: Clock,
        pools: dict[str, int] | None = None,
        lease_seconds: int = 3900,
        serves: Callable[[str], bool] | None = None,
    ) -> None:
        self.serves = serves or (lambda repo: True)
        self.store = store
        self.playbooks = {p.name: p for p in playbooks}
        self._context = context
        self.pipeline = pipeline
        self.profiles = profiles
        self.bus = bus
        self.clock = clock
        self.pools = pools or {"io": 8, "agent": 3, "lab": 1}
        self.lease_seconds = lease_seconds
        self._wake: dict[str, asyncio.Event] = {p: asyncio.Event() for p in self.pools}
        self._tasks: list[asyncio.Task[None]] = []
        self._running: set[asyncio.Task[None]] = set()
        self._job_tasks: dict[str, asyncio.Task[object]] = {}
        self._stopped: dict[str, str] = {}
        self._stopping = False
        """Set while the service stops: the jobs it cancels go back to the queue."""
        self.on_change: Callable[[Job], None] | None = None
        """Called whenever a job's status changes, for the dashboard."""

    # -- events → jobs ---------------------------------------------------------------

    async def handle_event(self, event: Event) -> list[Job]:
        """Wake waiting jobs that match ``event`` and start the playbooks it triggers."""
        if (
            event.kind == EventKind.PR_SYNCHRONIZED
            and event.repo
            and event.number
            and event.sha
        ):
            await self.pipeline.supersede_stale(event.repo, event.number, event.sha)
        if event.kind == EventKind.MEMORY_CHANGED:
            self.profiles.invalidate(event.repo)
        await self._wake_waiting(event)

        created: list[Job] = []
        if event.kind == EventKind.SCHEDULE_TICK and event.repo is None:
            name = event.payload.get("name")
            repos = [
                r.repo
                for r in await self.store.list_repos()
                if r.active and self.serves(r.repo)
            ]
            for playbook in self.playbooks.values():
                if name in playbook.tick_names:
                    for repo in repos:
                        created += await self._start(
                            playbook, event.model_copy(update={"repo": repo})
                        )
            return created
        for playbook in self.playbooks.values():
            if event.kind in playbook.triggers:
                created += await self._start(playbook, event)
        return created

    async def _start(self, playbook: Playbook, event: Event) -> list[Job]:
        profile = None
        if event.repo:
            profile = await self.profiles.active(event.repo)
            record = await self.store.get_repo(event.repo)
            if playbook.needs_profile and (
                profile is None or record is None or not record.active
            ):
                return []
            if profile is not None and not profile.playbook_enabled(playbook.name):
                return []
        elif playbook.needs_profile:
            return []
        if not playbook.accepts(event, profile):
            return []
        job = Job(
            playbook=playbook.name,
            key=playbook.key(event),
            repo=event.repo,
            number=event.number,
            pool=playbook.pool,
            events=[event.model_dump(mode="json")],
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        stored, new = await self.store.upsert_job(job)
        if new or stored.status == JobStatus.QUEUED:
            self._wake.setdefault(stored.pool, asyncio.Event()).set()
        return [stored] if new else []

    async def _wake_waiting(self, event: Event) -> None:
        for job in await self.store.list_jobs([JobStatus.WAITING]):
            wait = job.wait
            if wait is None or event.kind not in wait.kinds:
                continue
            if event.kind in _ACTION_EVENTS and wait.action_ids:
                if event.payload.get("action_id") not in wait.action_ids:
                    continue
            elif wait.repo and (
                event.repo != wait.repo
                or (wait.number is not None and event.number != wait.number)
            ):
                continue
            job.events = (job.events + [event.model_dump(mode="json")])[-20:]
            job.status = JobStatus.QUEUED
            job.wait = None
            job.updated_at = self.clock.now()
            await self.store.save_job(job)
            self._wake.setdefault(job.pool, asyncio.Event()).set()

    # -- workers -----------------------------------------------------------------------

    async def recover(self) -> int:
        """Queue again the jobs a previous process left running, before this one
        starts: it stopped without putting them back (a crash, or a kill). The attempt
        they were on counts, so a job that keeps bringing the service down runs out of
        attempts. Returns how many it queued or, out of attempts, failed."""
        now = self.clock.now()
        left = await self.store.list_jobs([JobStatus.RUNNING], limit=100_000)
        for job in left:
            job.status = (
                JobStatus.QUEUED
                if job.attempts < job.max_attempts
                else JobStatus.FAILED
            )
            job.error = "the service stopped while it ran"
            job.lease_until = None
            job.updated_at = now
            await self.store.save_job(job)
        return len(left)

    def start(self) -> None:
        self._stopping = False
        for pool, size in self.pools.items():
            self._tasks.append(
                asyncio.create_task(self._pool_loop(pool, size), name=f"pool:{pool}")
            )

    async def stop(self) -> None:
        """Stop working. Running jobs are cancelled and go back to the queue, so the
        next start (after a deploy, for example) runs them again at once."""
        self._stopping = True
        for task in self._tasks + list(self._running):
            task.cancel()
        for task in self._tasks + list(self._running):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _pool_loop(self, pool: str, size: int) -> None:
        sem = asyncio.Semaphore(size)
        wake = self._wake.setdefault(pool, asyncio.Event())
        while True:
            claimed = await self.store.claim_jobs(
                pool, size, self.lease_seconds, self.clock.now()
            )
            if not claimed:
                wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=5)
                continue
            for job in claimed:
                await sem.acquire()
                task = asyncio.create_task(
                    self._guarded(job, sem), name=f"job:{job.playbook}"
                )
                self._running.add(task)
                task.add_done_callback(self._running.discard)

    async def _guarded(self, job: Job, sem: asyncio.Semaphore) -> None:
        try:
            await self.run_job(job)
        finally:
            sem.release()

    def _changed(self, job: Job) -> None:
        if self.on_change is not None:
            try:
                self.on_change(job)
            except Exception:
                log.exception("job change listener failed")

    async def stop_job(self, job_id: str, by: str) -> Job | None:
        """Stop a job that has not finished: cancel it if it runs (its agent process
        ends and its checkout is released), otherwise mark it cancelled. Returns the
        job as it is afterwards, or ``None`` when there is no such job."""
        job = await self.store.get_job(job_id)
        if job is None or job.status in (
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            return job
        task = self._job_tasks.get(job_id)
        if task is not None and not task.done():
            self._stopped[job_id] = by
            task.cancel()
            await asyncio.wait({task}, timeout=30)
            return await self.store.get_job(job_id)
        job.status = JobStatus.CANCELLED
        job.error = f"stopped by {by}"
        job.lease_until = None
        job.wait = None
        job.updated_at = self.clock.now()
        await self.store.save_job(job)
        self._changed(job)
        return job

    async def run_job(self, job: Job) -> PlaybookResult | None:
        current = asyncio.current_task()
        if current is not None:
            self._job_tasks[job.id] = current
        try:
            return await self._run_job(job)
        except asyncio.CancelledError:
            by = self._stopped.pop(job.id, None)
            if by is None:
                if self._stopping:
                    await self._requeue_for_restart(job)
                raise
            if current is not None:
                current.uncancel()
            job.status = JobStatus.CANCELLED
            job.error = f"stopped by {by}"
            job.lease_until = None
            job.updated_at = self.clock.now()
            await self.store.save_job(job)
            self._changed(job)
            return None
        finally:
            self._job_tasks.pop(job.id, None)

    async def _requeue_for_restart(self, job: Job) -> None:
        """Put a job the service stopped back in the queue, without counting the
        attempt the stop ended: it runs again, from fresh reads, once the service is
        back."""
        current = await self.store.get_job(job.id) or job
        if current.status != JobStatus.RUNNING:
            return
        current.status = JobStatus.QUEUED
        current.attempts = max(0, current.attempts - 1)
        current.lease_until = None
        current.error = None
        current.updated_at = self.clock.now()
        await self.store.save_job(current)

    async def _run_job(self, job: Job) -> PlaybookResult | None:
        self._changed(job)
        playbook = self.playbooks.get(job.playbook)
        if playbook is None:
            job.status, job.error = JobStatus.FAILED, f"unknown playbook {job.playbook}"
            await self.store.save_job(job)
            return None
        event = (
            Event.model_validate(job.events[-1])
            if job.events
            else Event(kind=EventKind.SCHEDULE_TICK)
        )
        try:
            ctx = await self._context(job, event)
            result = await playbook.run(ctx, event)
        except Exception as e:
            log.exception("playbook %s failed on %s", job.playbook, job.key)
            job.error = f"{type(e).__name__}: {e}"
            if job.attempts < job.max_attempts:
                job.status = JobStatus.QUEUED
                job.not_before = self.clock.now() + timedelta(
                    seconds=30 * 2**job.attempts
                )
            else:
                job.status = JobStatus.FAILED
            job.lease_until = None
            job.updated_at = self.clock.now()
            await self.store.save_job(job)
            self._changed(job)
            return None

        for action in result.actions:
            action.provenance.job_id = job.id
            action.provenance.playbook = job.playbook
            await self.pipeline.submit(action)
        for signal in result.signals:
            await self.store.add_signal(signal)
        now = self.clock.now()
        job.result = result.result or None
        job.error = result.failed
        job.lease_until = None
        job.updated_at = now
        if result.failed is not None:
            job.status = JobStatus.FAILED
        elif result.wait is not None:
            job.status = JobStatus.WAITING
            job.attempts = 0
            job.wait = WaitSpec(
                kinds=result.wait.kinds,
                repo=job.repo,
                number=job.number,
                action_ids=result.wait.action_ids,
                deadline=now + timedelta(seconds=result.wait.seconds),
                reason=result.wait.reason,
            )
        elif result.retry_after is not None:
            job.status = JobStatus.QUEUED
            job.attempts = 0
            job.not_before = now + timedelta(seconds=result.retry_after)
        else:
            job.status = JobStatus.DONE
        await self.store.save_job(job)
        self._changed(job)
        for e in result.events:
            await self.bus.publish(e)
        return result

    async def tick(self) -> None:
        """Requeue expired leases and wake jobs whose wait deadline passed."""
        now = self.clock.now()
        await self.store.requeue_expired(now)
        for job in await self.store.list_jobs([JobStatus.WAITING]):
            if job.wait and job.wait.deadline <= now:
                timeout = Event(
                    kind=EventKind.JOB_TIMEOUT,
                    repo=job.repo,
                    number=job.number,
                    payload={"reason": job.wait.reason},
                )
                job.events = (job.events + [timeout.model_dump(mode="json")])[-20:]
                job.status = JobStatus.QUEUED
                job.wait = None
                job.updated_at = now
                await self.store.save_job(job)
                self._wake.setdefault(job.pool, asyncio.Event()).set()

    async def run_until_idle(self, max_rounds: int = 50) -> int:
        """Run queued jobs inline until none are left; for tests and ``am`` one-shot commands."""
        ran = 0
        for _ in range(max_rounds):
            progressed = False
            for pool in self.pools:
                for job in await self.store.claim_jobs(
                    pool, 100, self.lease_seconds, self.clock.now()
                ):
                    await self.run_job(job)
                    ran += 1
                    progressed = True
            drain = getattr(self.bus, "drain", None)
            if drain:
                await drain()
            actuator_drain = getattr(self.pipeline.actuator, "drain", None)
            if actuator_drain:
                await actuator_drain()
                if drain:
                    await drain()
            if not progressed:
                break
        return ran
