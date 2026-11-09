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

"""The actuator (design 5.9): executes allowed actions exactly once, one queue per repository."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging

from ..domain.actions import ActionKind, ActionStatus, ProposalState
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.markers import parse_markers
from ..domain.memory import MemoryChangeState
from ..domain.records import ActionRecord, AuditEntry
from ..domain.snapshot import PrState
from ..policy.leak_guard import LeakError
from .context import ExecContext
from .handlers import HANDLERS, Noop, Stale
from .memory_gate import try_merge_memory

log = logging.getLogger(__name__)

AS_PERSON = frozenset(
    {
        ActionKind.REVIEW_POST,
        ActionKind.REVIEW_UPDATE,
        ActionKind.PR_FIX,
        ActionKind.PR_UPDATE_BRANCH,
        ActionKind.PR_APPROVE,
        ActionKind.CI_APPROVE_RUN,
    }
)
"""Action kinds a maintainer may run with their own GitHub account."""

ONLY_AS_PERSON = frozenset(
    {ActionKind.PR_FIX, ActionKind.PR_APPROVE, ActionKind.CI_APPROVE_RUN}
)
"""Kinds that run only as the person who approves them, never as the service."""

ON_TEST_MACHINES = frozenset(
    {ActionKind.LAB_REPRODUCE, ActionKind.LAB_REVIEW, ActionKind.LAB_RUN}
)
"""Actions that hold a test machine, for up to an hour: each repository runs them in
a lane of their own, so its reviews, labels, and comments go out meanwhile."""

_COMMENT_KINDS = {
    ActionKind.REVIEW_POST,
    ActionKind.COMMENT_UPSERT,
    ActionKind.PR_NUDGE,
    ActionKind.ISSUE_COMMENT,
}


class Actuator:
    def __init__(self, x: ExecContext) -> None:
        self.x = x
        self._queues: dict[str, asyncio.Queue[tuple[str, str | None]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._pending = 0

    def enqueue(
        self,
        action_id: str,
        repo: str,
        approver: str | None = None,
        kind: ActionKind | None = None,
    ) -> None:
        """Queue an action. A repository's actions run one at a time, in order,
        except those on its test machines (``ON_TEST_MACHINES``), which run one at a
        time in a lane of their own."""
        lane = f"{repo} on test machines" if kind in ON_TEST_MACHINES else repo
        queue = self._queues.get(lane)
        if queue is None:
            queue = self._queues[lane] = asyncio.Queue()
            self._workers[lane] = asyncio.create_task(
                self._worker(queue), name=f"actuator:{lane}"
            )
        self._pending += 1
        self._idle.clear()
        queue.put_nowait((action_id, approver))

    async def _worker(self, queue: asyncio.Queue[tuple[str, str | None]]) -> None:
        while True:
            action_id, approver = await queue.get()
            try:
                await self.execute(action_id, approver)
            except Exception:
                log.exception("actuator crashed on %s", action_id)
            finally:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.set()
                queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued action ran; for tests and shutdown."""
        await self._idle.wait()

    async def stop(self) -> None:
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def execute(
        self, action_id: str, approver: str | None = None
    ) -> ActionRecord | None:
        record = await self.x.store.get_action(action_id)
        if record is None:
            return None
        if record.status in (
            ActionStatus.EXECUTED,
            ActionStatus.NOOP,
            ActionStatus.SUPERSEDED,
        ):
            return record
        action = record.action
        spec = HANDLERS[action.kind]
        profile = await self.x.profiles.active(action.repo)
        if profile is None and action.kind.touches_repository:
            return await self._finish(
                record,
                ActionStatus.FAILED,
                error=f"{action.repo} has no approved profile",
            )
        pr = None
        running = False  # the handler started, and may have written
        try:
            if spec.needs_pr:
                if action.number is None:
                    raise RuntimeError(f"{action.kind} needs a PR number")
                pr = await self.x.reader.get_pr(action.repo, action.number)
                if pr is None or pr.state != PrState.OPEN:
                    raise Stale(f"{action.repo}#{action.number} is no longer open")
                if (
                    spec.needs_current_head
                    and action.head_sha
                    and not same_commit(pr.head_sha, action.head_sha)
                    and not (spec.approved_on_its_commit and approver)
                ):
                    raise Stale(
                        f"the head moved from {action.head_sha[:7]} to {pr.head_sha[:7]}"
                    )
            record.status = ActionStatus.INTENT
            record.approver = approver or record.approver
            await self.x.store.save_action(record)
            x = self.x
            if action.kind in ONLY_AS_PERSON and not record.post_as:
                raise RuntimeError(
                    f"{action.kind} runs only as the person who approves it"
                )
            if action.kind == ActionKind.REVIEW_UPDATE and record.post_as != (
                action.payload.get("author") or None
            ):
                raise RuntimeError(
                    "a posted review is edited only by whoever posted it: "
                    f"{action.payload.get('author') or 'the service'}"
                )
            if record.post_as and action.kind in AS_PERSON:
                writer = await x.writer_as(record.post_as) if x.writer_as else None
                if writer is None:
                    raise RuntimeError(
                        f"{record.post_as}'s GitHub sign-in cannot post here any more; "
                        "sign in again"
                    )
                x = dataclasses.replace(x, writer=writer)
                if action.kind == ActionKind.PR_FIX:
                    publisher = (
                        await x.publisher_as(record.post_as) if x.publisher_as else None
                    )
                    if publisher is None:
                        raise RuntimeError(
                            f"{record.post_as}'s GitHub sign-in cannot push here; sign in again"
                        )
                    x = dataclasses.replace(x, publisher=publisher)
            running = True
            ref = await spec.run(x, action, profile, pr, record.approver)
            if record.post_as:
                ref = {**ref, "posted_as": record.post_as}
        except Noop as e:
            return await self._finish(record, ActionStatus.NOOP, error=str(e))
        except Stale as e:
            return await self._finish(record, ActionStatus.SUPERSEDED, error=str(e))
        except LeakError as e:
            # Handlers build all their public text before they write anything.
            return await self._finish(
                record, ActionStatus.FAILED, error=f"leak guard: {e}", unwritten=True
            )
        except Exception as e:
            log.exception("action %s failed", action.id)
            return await self._finish(
                record,
                ActionStatus.FAILED,
                error=f"{type(e).__name__}: {e}",
                unwritten=not running,
            )
        return await self._finish(record, ActionStatus.EXECUTED, ref=ref)

    async def _finish(
        self,
        record: ActionRecord,
        status: ActionStatus,
        ref: dict | None = None,
        error: str | None = None,
        unwritten: bool = False,
    ) -> ActionRecord:
        """Record how ``record`` ended. A proposal whose action failed before it wrote
        anything (``unwritten``) goes back to its reviewers as it was, with the reason
        among its warnings, to edit and approve again; the action runs again then."""
        record.status = status
        record.external_ref = ref or record.external_ref
        record.error = error
        record.updated_at = self.x.clock.now()
        await self.x.store.save_action(record)
        action = record.action
        proposal = await self.x.store.proposal_for_action(action.id)
        if (
            proposal is not None
            and status == ActionStatus.FAILED
            and unwritten
            and proposal.state == ProposalState.APPROVED
        ):
            proposal.state = ProposalState.PENDING
            proposal.decided_by = proposal.decided_at = None
            proposal.warnings = [
                *(w for w in proposal.warnings if not w.startswith("not done: ")),
                f"not done: {error}",
            ]
            await self.x.store.save_proposal(proposal)
        elif proposal is not None and proposal.state in (
            ProposalState.APPROVED,
            ProposalState.PENDING,
        ):
            proposal.state = {
                ActionStatus.EXECUTED: ProposalState.EXECUTED,
                ActionStatus.NOOP: ProposalState.EXECUTED,
                ActionStatus.SUPERSEDED: ProposalState.SUPERSEDED,
            }.get(status, ProposalState.FAILED)
            await self.x.store.save_proposal(proposal)
        await self.x.store.add_audit(
            AuditEntry(
                kind="execution",
                repo=action.repo,
                number=action.number,
                actor=record.approver or "service",
                action_id=action.id,
                summary=f"{action.kind} {status}" + (f": {error}" if error else ""),
                data={"ref": record.external_ref},
            )
        )
        kind = (
            EventKind.ACTION_EXECUTED
            if status in (ActionStatus.EXECUTED, ActionStatus.NOOP)
            else EventKind.ACTION_FAILED
        )
        await self.x.bus.publish(
            Event(
                kind=kind,
                repo=action.repo,
                number=action.number,
                sha=action.head_sha,
                payload={
                    "action_id": action.id,
                    "kind": action.kind,
                    "status": status,
                    "ref": record.external_ref,
                    "error": error,
                },
            )
        )
        return record

    async def reconcile_intents(self) -> int:
        """On start: settle actions left between intent and result (design 4.10), and
        queue again, in the order they were allowed, the allowed actions that waited
        in the queue, which lives only in memory, when the service stopped."""
        records = await self.x.store.list_actions(statuses=[ActionStatus.INTENT])
        waiting = sorted(
            await self.x.store.list_actions(statuses=[ActionStatus.QUEUED]),
            key=lambda r: r.action.created_at,
        )
        settled = 0
        for record in records:
            action = record.action
            done = False
            if action.kind in _COMMENT_KINDS and action.number is not None:
                pr = await self.x.reader.get_pr(action.repo, action.number)
                bodies = [c.body for c in (pr.comments if pr else [])] + [
                    r.body for r in (pr.reviews if pr else [])
                ]
                done = any(
                    m.action == action.id for b in bodies for m in parse_markers(b)
                )
            if done:
                await self._finish(
                    record, ActionStatus.EXECUTED, ref={"reconciled": True}
                )
            else:
                record.status = ActionStatus.QUEUED
                await self.x.store.save_action(record)
                self.enqueue(action.id, action.repo, record.approver, action.kind)
            settled += 1
        for record in waiting:
            self.enqueue(
                record.action.id,
                record.action.repo,
                record.approver,
                record.action.kind,
            )
            settled += 1
        return settled

    async def sweep_memory(self) -> int:
        """Merge open memory changes whose gate is now satisfied (notice windows, approvals)."""
        merged = 0
        for change in await self.x.store.list_memory_changes(MemoryChangeState.OPEN):
            after = await try_merge_memory(self.x, change)
            merged += after.state == MemoryChangeState.MERGED
        return merged

    async def merge_memory(self, change_id: str) -> str | None:
        change = await self.x.store.get_memory_change(change_id)
        if change is None:
            return None
        change = await try_merge_memory(self.x, change)
        return change.state
