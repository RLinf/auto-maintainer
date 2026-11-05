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

"""The memory gate (design 2.3): stage memory changes and merge the ones that may merge."""

from __future__ import annotations

import logging

from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, MemoryChangeState, Tier, lock_path
from ..domain.records import AuditEntry
from ..profiles.gate import memory_change_ready
from ..profiles.service import finalize_lock_files
from .context import ExecContext

log = logging.getLogger(__name__)


async def try_merge_memory(x: ExecContext, change: MemoryChange) -> MemoryChange:
    """Stage ``change`` if needed, and merge it when the memory gate allows."""
    if change.state != MemoryChangeState.OPEN:
        return change
    if not change.branch:
        change.branch = await x.memory.stage_change(change)
        await x.store.save_memory_change(change)
    remote = x.memory.remote_repo()
    if remote and change.pr_number is None and change.tier != Tier.T0:
        try:
            approvals = (
                ", ".join(f"{r}: {', '.join(v)}" for r, v in change.approvals.items())
                or "none yet"
            )
            body = f"{change.description}\n\nTier: {change.tier}. Repositories: {', '.join(change.repos)}. Approvals: {approvals}."
            ref = await x.writer.create_service_pr(
                remote, change.branch, "main", change.title, body
            )
            change.pr_number = ref.id
            await x.store.save_memory_change(change)
        except Exception as e:  # the dashboard path still works without the PR
            log.warning("could not open a memory PR for %s: %s", change.id, e)

    auto: dict[str, bool] = {}
    notice: dict[str, int] = {}
    for repo in change.repos:
        profile = await x.profiles.active(repo)
        auto[repo] = bool(profile and profile.memory.knowledge_auto_merge)
        notice[repo] = profile.memory.notice_hours if profile else 24
    ready, reason = memory_change_ready(change, auto, notice, x.clock.now())
    if not ready:
        log.info("memory change %s waits: %s", change.id, reason)
        return change

    now = x.clock.now()
    if change.tier == Tier.T2:
        finalized = finalize_lock_files(change, now)
        if finalized.files != change.files:
            change = finalized
            change.branch = await x.memory.stage_change(change)
    try:
        sha = await x.memory.merge_change(change)
    except Exception as e:  # a conflict with a maintainer's edit: theirs wins
        change.state = MemoryChangeState.CONFLICT
        change.rejected_reason = str(e)
        await x.store.save_memory_change(change)
        await x.store.add_audit(
            AuditEntry(
                kind="memory",
                summary=f"conflict merging {change.id}: {e}",
                data={"change": change.id},
            )
        )
        return change
    change.state = MemoryChangeState.MERGED
    change.merged_at = now
    await x.store.save_memory_change(change)
    await x.store.add_audit(
        AuditEntry(
            kind="memory",
            summary=f"merged {change.tier} change: {change.title}",
            data={
                "change": change.id,
                "sha": sha,
                "approvals": change.approvals,
                "reason": reason,
            },
        )
    )
    for repo in change.repos or [None]:
        if repo:
            x.profiles.invalidate(repo)
            if lock_path(repo) in change.files:
                record = await x.store.get_repo(repo)
                if record and not record.active:
                    record.active = True
                    await x.store.save_repo(record)
        await x.bus.publish(
            Event(
                kind=EventKind.MEMORY_CHANGED,
                repo=repo,
                payload={
                    "change": change.id,
                    "sha": sha,
                    "paths": sorted(change.files),
                },
            )
        )
    return change
