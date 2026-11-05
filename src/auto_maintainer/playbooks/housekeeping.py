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

"""Stale and SLA tracking, lab tests, and the journal (design 3.2 and 5.11)."""

from __future__ import annotations

import json

from ..domain.actions import (
    ActionHints,
    ActionKind,
    LabelPayload,
    LabRunPayload,
    MemoryCommitPayload,
    NudgePayload,
    PrStatePayload,
    make_action,
)
from ..domain.calendar import BusinessCalendar
from ..domain.events import Event, EventKind
from ..domain.memory import journal_path
from ..domain.profile import RepoProfile
from ..domain.review import ReviewState
from ..domain.snapshot import PrSnapshot, ReviewEventState
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope


def _last_activity(pr: PrSnapshot, logins: set[str] | None = None) -> object:
    times = [pr.updated_at, pr.created_at]
    times += [
        r.submitted_at for r in pr.reviews if logins is None or r.author in logins
    ]
    times += [c.created_at for c in pr.comments if logins is None or c.author in logins]
    return max(t for t in times if t is not None)


class Stale(Playbook):
    name = "stale"
    triggers = ()
    tick_names = ("stale",)

    def key(self, event: Event) -> str:
        return f"stale:{event.repo}:{event.created_at:%Y-%m-%d}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        cal = BusinessCalendar(profile.timing)
        now = ctx.clock.now()
        actions = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            if not in_scope(pr, profile) and not pr.is_draft:
                continue
            if pr.is_draft:
                idle = cal.business_days_between(_last_activity(pr), now)  # type: ignore[arg-type]
                if idle >= profile.timing.stale_days:
                    actions.append(
                        make_action(
                            ActionKind.PR_CLOSE,
                            pr.repo,
                            PrStatePayload(
                                body=f"Closing this draft after {idle} business days without activity. Reopen it when you pick it up again."
                            ),
                            number=pr.number,
                            head_sha=pr.head_sha,
                            discriminator=f"{now:%Y-%m-%d}",
                        )
                    )
                continue
            state = ctx.classify(pr).state
            humans = {r.author for r in pr.reviews if r.author not in ctx.identities()}
            waiting = cal.business_days_between(pr.created_at or now, now)
            if (
                state == ReviewState.NEW_REVIEW
                and not humans
                and waiting >= profile.timing.first_response_days
            ):
                owners = [
                    o
                    for o in await ctx.codeowners_for(pr.paths)
                    if o.lower() != pr.author.lower()
                ][:3]
                if owners:
                    mention = " ".join(f"@{o}" for o in owners)
                    actions.append(
                        make_action(
                            ActionKind.PR_NUDGE,
                            pr.repo,
                            NudgePayload(
                                body=f"{mention}: this PR has waited {waiting} business days for a first review.",
                                audience="reviewers",
                            ),
                            number=pr.number,
                            head_sha=pr.head_sha,
                            discriminator=f"first:{now:%Y-%m-%d}",
                        )
                    )
                continue
            changes = [
                r for r in pr.reviews if r.state == ReviewEventState.CHANGES_REQUESTED
            ]
            label = profile.label_for("changes_requested")
            if changes or (label and label in pr.labels):
                idle = cal.business_days_between(_last_activity(pr, {pr.author}), now)  # type: ignore[arg-type]
                if idle >= profile.timing.rereview_days * 3:
                    actions.append(
                        make_action(
                            ActionKind.PR_NUDGE,
                            pr.repo,
                            NudgePayload(
                                body=f"@{pr.author}: are you still working on this? The requested changes have waited {idle} business days.",
                                audience="author",
                            ),
                            number=pr.number,
                            head_sha=pr.head_sha,
                            discriminator=f"author:{now:%Y-%m-%d}",
                        )
                    )
        return PlaybookResult(actions=actions, result={"nudges": len(actions)})


class LabTest(Playbook):
    """Run a profile's lab template for a PR; on success, apply the ``reproduced`` role."""

    name = "lab_test"
    triggers = (EventKind.DASHBOARD_COMMAND, EventKind.ACTION_EXECUTED)
    pool = "lab"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if event.kind == EventKind.ACTION_EXECUTED:
            return event.payload.get("kind") == ActionKind.LAB_RUN and bool(
                event.payload.get("ref", {}).get("passed")
            )
        return event.payload.get("command") == "lab_test" and event.number is not None

    def key(self, event: Event) -> str:
        return f"lab_test:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None:
            return PlaybookResult(result={"skipped": "PR not found"})
        hints = ActionHints(
            author_is_team=await ctx.author_is_team(pr), changed_paths=pr.paths
        )
        if event.kind == EventKind.ACTION_EXECUTED:
            if not profile.label_for("reproduced"):
                return PlaybookResult()
            return PlaybookResult(
                actions=[
                    make_action(
                        ActionKind.LABEL_APPLY,
                        pr.repo,
                        LabelPayload(roles=["reproduced"]),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator="lab",
                        hints=hints,
                    )
                ]
            )
        lab = event.payload.get("lab") or next(iter(profile.labs), None)
        template = event.payload.get("template")
        if lab not in profile.labs or template not in profile.labs[lab].templates:
            return PlaybookResult(
                result={"error": f"unknown lab {lab!r} or template {template!r}"}
            )
        action = make_action(
            ActionKind.LAB_RUN,
            pr.repo,
            LabRunPayload(
                lab=lab,
                template=template,
                args={
                    str(k): str(v) for k, v in (event.payload.get("args") or {}).items()
                },
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=event.id,
            hints=hints,
        )
        action.provenance.requested_by = event.payload.get("by")
        return PlaybookResult(actions=[action])


class Journal(Playbook):
    """Append the day's signals to the repository's journal (T0) and mark them consumed."""

    name = "journal"
    triggers = ()
    tick_names = ("learn",)

    def key(self, event: Event) -> str:
        return f"journal:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        signals = await ctx.store.list_signals(profile.repo, unconsumed=True)
        if not signals:
            return PlaybookResult(result={"signals": 0})
        lines = [
            json.dumps(s.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for s in signals
        ]
        action = make_action(
            ActionKind.MEMORY_COMMIT,
            profile.repo,
            MemoryCommitPayload(
                path=journal_path(profile.repo, ctx.clock.now()),
                lines=lines,
                message=f"journal: {len(lines)} signals for {profile.repo}",
            ),
            discriminator=event.id,
        )
        for s in signals:
            s.consumed_by = ctx.job.id
            await ctx.store.save_signal(s)
        return PlaybookResult(actions=[action], result={"signals": len(signals)})
