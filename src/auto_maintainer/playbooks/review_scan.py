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

"""The periodic PR scan (``review.trigger: scan``): review a bounded batch on a schedule.

Each tick checks the repository's own ``review.scan.schedule``. When it is due, the
scan picks the PRs that need a first review or a follow-up and have no review draft
at their current head yet, highest priority first, and requests reviews for at most
``review.scan.max_prs`` of them. Drafts from a scan follow the repository's policy.
"""

from __future__ import annotations

from datetime import datetime

from croniter import croniter

from ..domain.actions import ActionKind, ActionStatus
from ..domain.clock import as_utc
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.profile import RepoProfile
from ..domain.review import ReviewState
from ..domain.snapshot import PrSnapshot
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .requests import FOLLOWUP_STATES

SCAN_STATE = "review_scan:{repo}"
_LIVE = [
    ActionStatus.SHADOWED,
    ActionStatus.PROPOSED,
    ActionStatus.QUEUED,
    ActionStatus.INTENT,
    ActionStatus.EXECUTED,
]


def scan_due(schedule: str, last: datetime | None, now: datetime) -> bool:
    """Whether a scan is due: never ran, or the schedule fired since the last run."""
    if last is None:
        return True
    return croniter(schedule, as_utc(last)).get_next(datetime) <= now


class ReviewScan(Playbook):
    name = "review_scan"
    triggers = ()
    tick_names = ("review_scan",)

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return profile is not None and profile.review.trigger == "scan"

    def key(self, event: Event) -> str:
        return f"review_scan:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        scan = profile.review.scan
        state_key = SCAN_STATE.format(repo=profile.repo)
        last_raw = await ctx.store.get_state(state_key)
        last = datetime.fromisoformat(last_raw) if last_raw else None
        now = ctx.clock.now()
        if not scan_due(scan.schedule, last, now):
            return PlaybookResult(result={"skipped": "not due"})
        record = await ctx.store.get_repo(profile.repo)
        candidates: list[tuple[float, PrSnapshot, str]] = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            if not in_scope(pr, profile):
                continue
            state = ctx.classify(pr).state
            if state == ReviewState.NEW_REVIEW:
                old = record and pr.created_at and pr.created_at < record.added_at
                if old and not profile.scope.backfill_reviews:
                    continue
            elif not (state in FOLLOWUP_STATES and scan.followups):
                continue
            if await self._has_draft(ctx, pr):
                continue
            evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
            candidates.append((evaluation.total if evaluation else 0.0, pr, str(state)))
        candidates.sort(key=lambda c: (-c[0], -c[1].number))
        picked = candidates[: scan.max_prs]
        events = [
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo=pr.repo,
                number=pr.number,
                sha=pr.head_sha,
                payload={"command": "review", "source": "scan"},
                source="scan",
            )
            for _, pr, _ in picked
        ]
        await ctx.store.set_state(state_key, now.isoformat())
        return PlaybookResult(
            events=events,
            result={
                "requested": [pr.number for _, pr, _ in picked],
                "candidates": len(candidates),
            },
        )

    async def _has_draft(self, ctx: PlaybookContext, pr: PrSnapshot) -> bool:
        records = await ctx.store.list_actions(
            repo=pr.repo,
            number=pr.number,
            kinds=[ActionKind.REVIEW_POST],
            statuses=_LIVE,
        )
        return any(same_commit(r.action.head_sha, pr.head_sha) for r in records)
