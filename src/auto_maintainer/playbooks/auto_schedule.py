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


"""Automatic merge scheduling (design 5.5): with ``merge.schedule.auto`` on, every PR
that becomes ready to merge is scheduled as the maintainer who turned it on.

It runs no agent: it reads the PR and the merge gate, as the Merge scheduling page
does. It schedules only while ``auto_by`` may still arm merges and configure the
repository, never a head a maintainer unscheduled (``merge_schedule.skip_key``), and
marks the arm ``auto``, so the steps that run outside code with the repository's
secrets wait for a person.
"""

from __future__ import annotations

from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.merge_gate import MergeArm
from ..domain.merge_schedule import holding_back, next_position, skip_key
from ..domain.profile import RepoProfile
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .merge import gate_for, maintains

AUTO_COMMAND = "merge_schedule"
"""The dashboard command that looks at one PR, sent for each open PR when a
maintainer turns automatic scheduling on."""


class AutoSchedule(Playbook):
    name = "merge_schedule"
    triggers = (
        EventKind.PR_OPENED,
        EventKind.PR_SYNCHRONIZED,
        EventKind.PR_UPDATED,
        EventKind.PR_LABELED,
        EventKind.PR_REVIEW_SUBMITTED,
        EventKind.PR_RECONCILED,
        EventKind.DASHBOARD_COMMAND,
    )

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if event.kind == EventKind.DASHBOARD_COMMAND and (
            event.payload.get("command") != AUTO_COMMAND
        ):
            return False
        return bool(
            profile
            and profile.merge.enabled
            and profile.merge.schedule.auto
            and profile.merge.schedule.auto_by
            and event.number
        )

    def key(self, event: Event) -> str:
        return f"merge_schedule:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        who = profile.merge.schedule.auto_by
        assert who
        if await ctx.store.get_arm(event.repo, event.number) is not None:
            return PlaybookResult(result={"skipped": "already scheduled"})
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "not open, or out of scope"})
        skipped = await ctx.store.get_state(skip_key(pr.repo, pr.number))
        if isinstance(skipped, str) and same_commit(skipped, pr.head_sha):
            return PlaybookResult(result={"skipped": "a maintainer unscheduled it"})
        permission = await ctx.permission(who)
        if not await maintains(ctx, profile, pr.repo, who, permission):
            return PlaybookResult(
                result={"skipped": f"{who} no longer maintains {pr.repo}"}
            )
        info = await ctx.forge.get_repo(pr.repo)
        gate = await gate_for(ctx, profile, info, pr, None)
        back = holding_back(gate)
        if back:
            return PlaybookResult(result={"not_ready": [i.name for i in back]})
        now = ctx.clock.now()
        arm = MergeArm(
            repo=pr.repo,
            number=pr.number,
            sha=pr.head_sha,
            armed_by=who,
            armed_at=now,
            armer_permission=permission,
            position=next_position(await ctx.store.list_arms(pr.repo), now),
            auto=True,
        )
        await ctx.store.save_arm(arm)
        return PlaybookResult(
            events=[
                Event(
                    kind=EventKind.MERGE_ARMED,
                    repo=pr.repo,
                    number=pr.number,
                    sha=pr.head_sha,
                    payload={"by": who, "armed_at": now.isoformat(), "auto": True},
                )
            ],
            result={"scheduled": pr.number, "as": who},
        )
