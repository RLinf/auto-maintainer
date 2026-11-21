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


"""Merge scheduling (design 5.5): one job per scheduled (armed) PR, from its turn to
its merge.

The PR waits for its turn in the repository's order (``domain/merge_schedule.py``);
on its turn it merges the base when it must be up to date and is behind, starts CI
the way the repository starts it (``ci.start``: the gate label, approving a fork's
held workflows), watches it, reruns what flakes within ``ci.rerun.max_per_sha``, and
merges once the merge gate holds. What really fails moves it to the failure lane
with its root cause, and the turn passes on. A stall (no run within
``ci.start.start_minutes``, or one past ``run_minutes``) gets one quick look from an
agent at each head. Every step is drafted for the maintainer who scheduled it and
approved by the schedule (``actuator/pipeline.py:SCHEDULED``). Each wake starts from
fresh reads; the phase is kept in the store (``merge_schedule.state_key``).

A PR that conflicts with its base gets, on its turn, the schedule's own resolution
when the repository allows it (``merge.schedule.resolve``): ``playbooks/merge_base``
resolves only minor conflicts, as the scheduler, and anything else sends the PR to
the failure lane. Before a step the forge would refuse, the schedule says why
instead of trying: a fork whose author lets no maintainer push to it, a merge GitHub
itself still reports blocked (``PrSnapshot.merge_state``) when the service's account
could merge past the rule that blocks it (``RepoInfo.service_bypasses_rules``; one
that cannot is refused by GitHub, which names the rule), a required check whose
workflow skips the PR (``domain/workflows.py``). A step the forge's rate limit
refused is taken again once the limit resets; a branch that merges only through
GitHub's merge queue is queued.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.access import Grant
from ..domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    ApproveRunsPayload,
    CancelRunsPayload,
    CommentUpsertPayload,
    LabelPayload,
    MergePayload,
    NotifyPayload,
    RerunPayload,
    UpdateBranchPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.forges import info_of
from ..domain.ids import same_commit, short_sha
from ..domain.merge_gate import (
    GateResult,
    MergeArm,
    evaluate_merge_gate,
    squash_message,
)
from ..domain.merge_schedule import (
    ACTIVE,
    FIX_SECONDS,
    FIXES_PER_SCHEDULE,
    LANDING_SECONDS,
    RESOLVE_SECONDS,
    UNKNOWN_READS,
    UPDATES_PER_TURN,
    WAITING,
    ScheduleState,
    fix_key,
    holding_back,
    left_for_merge,
    next_position,
    only_base_merged,
    ordered,
    ready_conditions,
    resolve_key,
    serial,
    state_key,
    turns,
)
from ..domain.permissions import Permission
from ..domain.profile import RepoProfile
from ..domain.records import ActionStatus, RootCause
from ..domain.run_ci import ci_view, latest_checks, rerun_targets, unfinished_runs
from ..domain.snapshot import (
    CheckConclusion,
    Mergeable,
    PrSnapshot,
    PrState,
    RepoInfo,
)
from ..domain.workflows import never_run
from .base import Playbook, PlaybookContext, PlaybookResult, Wait
from .ci_doctor import (
    explained,
    failing_checks,
    find_causes,
    first_looks,
    record,
    reruns_at_head,
)
from .hygiene import hygiene_key
from .merge_base import RESOLVE_COMMAND
from .prompts import schema_hint, untrusted

WAIT_KINDS = [
    EventKind.CHECKS_COMPLETED,
    EventKind.PR_REVIEW_SUBMITTED,
    EventKind.PR_SYNCHRONIZED,
    EventKind.PR_LABELED,
    EventKind.PR_UPDATED,
    EventKind.PR_CLOSED,
    EventKind.MERGE_DISARMED,
    EventKind.ACTION_EXECUTED,
    EventKind.ACTION_FAILED,
    EventKind.PR_RECONCILED,
    EventKind.MERGE_QUEUE,
]
RATE_LIMITED = "GitHub's rate limit"
"""How the reason of a step the forge's rate limit refused starts."""
WAIT_SECONDS = 600
"""How long a scheduled PR's job waits for an event (CI, a review, a push) before it
reads the PR again anyway: what it waits for can come without one (a conflict
GitHub works out later, a check reported late), and a read costs no agent."""
RUNNING_POLL_SECONDS = 120
"""How often a scheduled PR whose CI is actively running is checked again when no
event arrives, so mid-run failures are diagnosed promptly."""
CANCEL_POLL_SECONDS = 30
"""How quickly the schedule checks back after cancelling an in-progress workflow
run for a flaky failure, so the rerun starts as soon as GitHub marks it cancelled."""
BLOCKED_SECONDS = 900
"""How often a PR blocked on something only a person mends is looked at again."""
RATE_LIMIT_SECONDS = 600
"""How long a step the forge's rate limit refused waits before it is taken again,
when the refusal does not say when the limit resets."""
NEVER_RUNS = "never runs for this PR"
"""How a finding about a required check whose workflow skips the PR reads."""
RULES_SECONDS = 900
"""How often a merge GitHub itself reports blocked is looked at again."""
BLOCK_LIFTERS = frozenset(
    {
        EventKind.PR_REVIEW_SUBMITTED,
        EventKind.CHECKS_COMPLETED,
        EventKind.PR_UPDATED,
        EventKind.PR_LABELED,
    }
)
"""Changes to a PR after which a merge the forge refused (not for a scope) is
tried again: what refused it may have changed with them."""
MERGED_KEY = "merge-schedule-merged:{repo}"
MERGED_KEPT = 30
"""How many merges the page lists under Merged lately."""

STALLED = """CI of pull request #{number} in {repo}, at head {sha}, {what} for {minutes} minutes. Say why, from the facts below, and the one step that gets it going: `add_label` (the gate label that starts the repository's CI is missing), `approve_runs` (GitHub holds workflows until a maintainer approves running them), `rerun` (a run at this head failed or was cancelled for no reason of the change: give its run id), `wait` (runs are queued for busy runners and will start, or are still working normally), or `maintainer` (only a maintainer can do it, such as a required check from an outside service that never reports: say what in `for_maintainer`). Judge only from what is given here: do not run commands, read files, or fetch anything."""


class StallFinding(BaseModel):
    """Why a scheduled PR's CI did not start or finish, and the step to take."""

    cause: str = Field(description="One or two sentences on why.")
    step: Literal["add_label", "approve_runs", "rerun", "wait", "maintainer"]
    run_id: int | None = Field(default=None, description="For rerun: the run id.")
    for_maintainer: str = Field(
        default="", description="For maintainer: what they must do."
    )


def render_gate(gate: GateResult) -> str:
    lines = ["### Merge checklist", ""]
    for item in gate.items:
        mark = "✅" if item.ok else ("⏳" if item.pending else "❌")
        lines.append(
            f"- {mark} {item.name.replace('_', ' ')}"
            + (f": {item.detail}" if item.detail else "")
        )
    return "\n".join(lines)


def _same_schedule(ctx: PlaybookContext, arm: MergeArm) -> bool:
    """Whether this job was started for ``arm`` (by its arming time), not for an
    earlier schedule of the same PR."""
    first = ctx.job.events[0] if ctx.job.events else {}
    at = (first.get("payload") or {}).get("armed_at")
    if not at:
        return True
    try:
        return datetime.fromisoformat(str(at)) == arm.armed_at
    except ValueError:
        return True


def _waiting_for(items: list) -> str:
    """What a PR whose CI passed still waits for, in one line."""
    return "; ".join(
        i.name.replace("_", " ") + (f": {i.detail}" if i.detail else "") for i in items
    )


async def load_state(ctx: PlaybookContext, repo: str, number: int) -> ScheduleState:
    raw = await ctx.store.get_state(state_key(repo, number))
    return (
        ScheduleState.model_validate(raw) if isinstance(raw, dict) else ScheduleState()
    )


class MergePlaybook(Playbook):
    name = "merge"
    triggers = (EventKind.MERGE_ARMED,)
    pool = "agent"

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return bool(profile and profile.merge.enabled and event.number)

    def key(self, event: Event) -> str:
        return f"merge:{event.repo}#{event.number}:{event.payload.get('armed_at', event.id)}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        repo, number = event.repo, event.number
        if (
            event.kind == EventKind.ACTION_EXECUTED
            and event.payload.get("kind") == ActionKind.PR_MERGE
            and not (event.payload.get("ref") or {}).get("queued")
        ):
            await ctx.store.delete_arm(repo, number)
            return await self._leave(ctx, profile, repo, number, merged=True)
        arm = await ctx.store.get_arm(repo, number)
        if arm is not None and not _same_schedule(ctx, arm):
            # Unscheduled and scheduled again: the newer schedule's job has the PR.
            return PlaybookResult(result={"done": "scheduled again since"})
        if arm is None or event.kind == EventKind.MERGE_DISARMED:
            return await self._leave(ctx, profile, repo, number)
        pr = await ctx.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            await ctx.store.delete_arm(repo, number)
            return await self._leave(
                ctx,
                profile,
                repo,
                number,
                merged=bool(pr and pr.state == PrState.MERGED),
                pr=pr,
            )
        state = await load_state(ctx, repo, number)
        if event.kind == EventKind.ACTION_FAILED:
            failed = await self._our_failure(ctx, event, arm)
            if failed:
                kind, why, scope = failed
                if why.startswith(RATE_LIMITED):
                    return await self._later(ctx, profile, pr, state, kind, why)
                if kind == ActionKind.PR_FIX and await self._is_fix(ctx, event):
                    # Pushing the schedule's fix failed: a person's turn.
                    await self._fix_noted(ctx, pr, "refused", why)
                    return await self._lane(ctx, profile, pr, arm, state, why)
                if kind == ActionKind.PR_FIX:
                    # Pushing the schedule's resolution failed: a person's turn.
                    await ctx.store.set_state(
                        resolve_key(repo, number),
                        {
                            "sha": pr.head_sha,
                            "outcome": "refused",
                            "why": why,
                            "at": ctx.clock.now().isoformat(),
                        },
                    )
                if kind in (ActionKind.PR_MERGE, ActionKind.PR_UPDATE_BRANCH):
                    # Not its CI's fault: it stays in the order, without the turn.
                    return await self._block(ctx, profile, pr, arm, state, why, scope)
                return await self._fail(ctx, profile, pr, arm, state, why)
        if not same_commit(arm.sha, pr.head_sha):
            moved = await self._new_head(ctx, profile, pr, arm, state, event)
            if isinstance(moved, PlaybookResult):
                return moved
            arm = moved
        info = await ctx.forge.get_repo(repo)
        gate = await gate_for(ctx, profile, info, pr, arm)
        wait = Wait(kinds=WAIT_KINDS, seconds=WAIT_SECONDS, reason="merge schedule")
        now = ctx.clock.now()
        if state.not_before is not None and now < state.not_before:
            # The forge's rate limit refused a step: take it again once it resets.
            return PlaybookResult(
                wait=wait.model_copy(
                    update={
                        "seconds": max(
                            30, int((state.not_before - now).total_seconds())
                        ),
                        "reason": "the forge's rate limit",
                    }
                ),
                result={"phase": state.phase},
            )
        conflict = next(
            (i for i in gate.items if i.name == "no_conflict" and not i.ok), None
        )
        resolution = await self._resolution(ctx, pr) if conflict is not None else None
        if state.retry and resolution and resolution.get("outcome") == "refused":
            # Sent back by a maintainer: the schedule tries its resolution again.
            await ctx.store.set_state(resolve_key(repo, number), None)
            resolution = None
        resolvable = (
            conflict is not None
            and profile.merge.schedule.resolve.enabled
            and (resolution or {}).get("outcome") != "refused"
        )
        if state.phase == "blocked" and same_commit(state.sha, pr.head_sha):
            started = ctx.service.started_at
            again = (
                state.retry
                or (
                    started is not None
                    and state.since is not None
                    and state.since < started
                )
                # Only a new sign-in mends a missing scope, never a change to the PR.
                or (
                    state.blocked_on not in ("workflow_scope", "person_scope")
                    and event.kind in BLOCK_LIFTERS
                )
                # GitHub's own verdict is read afresh at each wake.
                or state.blocked_on == "forge_rules"
                # A base merge refused for a conflict the schedule may resolve.
                or (
                    resolvable
                    and state.blocked_on not in ("workflow_scope", "person_scope")
                )
                # A base merge that waited for its scheduler to sign in.
                or (
                    state.blocked_on == "scheduler_sign_in"
                    and await self._signed_in(ctx, arm.armed_by)
                )
            )
            if not again:
                return PlaybookResult(
                    actions=await self._quiet(ctx, profile, info, pr, arm, state),
                    wait=wait.model_copy(
                        update={"seconds": BLOCKED_SECONDS, "reason": "merge blocked"}
                    ),
                    result={"phase": "blocked"},
                )
            state.retry = False
            state.phase = "queued"
            state.detail = "trying its merge again"
        if state.phase == "fixing" and same_commit(state.sha, pr.head_sha):
            return await self._fixing(ctx, profile, info, pr, arm, state, wait)
        if state.phase == "failed" and same_commit(state.sha, pr.head_sha):
            ran = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start).state
            # Other checks may still run after a run that ended failed it: only the
            # failed checks running again bring it back.
            still = {c.id for c in failing_checks(pr, profile)} & set(state.failed_ids)
            if (
                not state.failed_on_ci
                or ran not in ("running", "passed")
                or still
                or state.retry
            ):
                return PlaybookResult(
                    actions=await self._quiet(ctx, profile, info, pr, arm, state),
                    wait=wait.model_copy(update={"reason": "in the failure lane"}),
                    result={"phase": "failed"},
                )
            # Someone ran its CI again (on the forge): it takes a turn again, after
            # every PR scheduled meanwhile.
            arm = await self._to_bottom(ctx, arm)
            state.phase = "queued"
            state.detail = "its CI ran again"
        if conflict is not None and not resolvable:
            why = (resolution or {}).get("why")
            return await self._fail(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"it conflicts with {pr.base_ref}"
                + (f", which the schedule did not resolve: {why}" if why else "")
                + f". Resolve the conflicts (Resolve conflicts here or on the PR "
                f"page has an agent merge {pr.base_ref} in for you to push): once "
                "the merge is pushed, it takes its turn again",
            )
        ready = ready_conditions(profile, arm)
        back = [
            i
            for i in holding_back(gate, ready)
            # A conflict the schedule resolves itself, on the PR's turn.
            if not (resolvable and i.name == "no_conflict")
        ]
        if back:
            first = back[0]
            detail = first.name.replace("_", " ") + (
                f": {first.detail}" if first.detail else ""
            )
            events = await self._set(
                ctx, profile, info, pr, state, "held", detail, wake=True
            )
            return PlaybookResult(
                actions=await self._quiet(
                    ctx, profile, info, pr, arm, state, _numbers(events)
                ),
                events=events,
                wait=wait.model_copy(update={"reason": f"held back: {detail}"}),
                result={"phase": "held", "failing": [i.name for i in back]},
            )
        paused = [
            i for i in gate.items if i.name in ("not_paused", "no_freeze") and not i.ok
        ]
        if paused:
            await self._set(ctx, profile, info, pr, state, "queued", paused[0].name)
            return PlaybookResult(
                wait=wait.model_copy(update={"reason": paused[0].name}),
                result={"phase": "queued", "failing": [paused[0].name]},
            )
        waiting = left_for_merge(gate, ready)
        if state.phase == "approval" and waiting:
            # Its CI passed; it takes a turn again once it meets what the merge
            # needs, so it neither holds the turn nor merges the base in meanwhile.
            await self._set(
                ctx, profile, info, pr, state, "approval", _waiting_for(waiting)
            )
            return PlaybookResult(
                actions=await self._quiet(ctx, profile, info, pr, arm, state),
                wait=wait.model_copy(update={"reason": "waiting for approvals"}),
                result={"phase": "approval"},
            )

        one = serial(info, profile)
        async with self._locks.setdefault(repo, asyncio.Lock()):
            arms = ordered(await ctx.store.list_arms(repo))
            order = [a.number for a in arms]
            states = {
                a.number: await load_state(ctx, repo, a.number)
                for a in arms
                if a.number != number
            }
            if state.phase in ("held", "failed", "merged", "approval"):
                state.phase = "queued"
            states[number] = state
            allowed = turns(order, states, one, profile.merge.schedule.parallel)
            if number not in allowed:
                ahead = [n for n in allowed if n != number]
                detail = f"after #{ahead[0]}" if ahead else "waiting for a turn"
                await self._set(ctx, profile, info, pr, state, "queued", detail)
                return PlaybookResult(
                    actions=await self._quiet(ctx, profile, info, pr, arm, state),
                    wait=wait.model_copy(
                        update={"reason": f"in the merge schedule; {detail}"}
                    ),
                    result={"phase": "queued", "turn": ahead[:1]},
                )
            if state.phase not in ACTIVE:
                # Take the turn before letting go of the lock.
                state.updates = 0
                await self._set(ctx, profile, info, pr, state, "starting", "its turn")
        return await self._act(
            ctx, profile, info, pr, arm, gate, state, wait, one, resolvable, resolution
        )

    async def _act(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        gate: GateResult,
        state: ScheduleState,
        wait: Wait,
        one: bool,
        resolvable: bool = False,
        resolution: dict[str, Any] | None = None,
    ) -> PlaybookResult:
        """The step a PR whose turn it is takes now."""
        now = ctx.clock.now()
        hints = ActionHints(
            author_is_team=await ctx.author_is_team(pr), changed_paths=pr.paths
        )
        # Each step the forge's rate limit refused is a new action when taken again,
        # and so is each step of a new schedule of the PR at the same head (its
        # attempts count afresh): an earlier failed one is never taken for it.
        again = f":t{state.transient}" if state.transient else ""
        armed = int(arm.armed_at.timestamp())

        def step(
            kind: ActionKind, payload: Any, discriminator: str, why: str
        ) -> Action:
            action = make_action(
                kind,
                pr.repo,
                payload,
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"schedule:{armed}:{discriminator}{again}",
                rationale=f"{why}; scheduled by {arm.armed_by}",
                draft_for=arm.armed_by,
                hints=hints,
            )
            action.provenance.scheduled_by = arm.armed_by
            action.provenance.requested_by = arm.armed_by
            return action

        if pr.mergeable == Mergeable.UNKNOWN and state.unknown_reads < UNKNOWN_READS:
            # GitHub works out whether a PR merges cleanly in the background, after
            # the base moves; a conflicting PR runs no CI, so CI is not started on a
            # guess: it is read again in a minute.
            state.unknown_reads += 1
            await self._set(
                ctx,
                profile,
                info,
                pr,
                state,
                state.phase if state.phase in ACTIVE else "starting",
                "waiting for GitHub to work out whether it merges cleanly",
            )
            return PlaybookResult(
                wait=wait.model_copy(
                    update={"seconds": 60, "reason": "mergeability unknown"}
                ),
                result={"phase": state.phase, "mergeable": "unknown"},
            )
        if pr.mergeable != Mergeable.UNKNOWN:
            state.unknown_reads = 0
        if resolvable:
            resolving = await self._resolve(
                ctx, profile, info, pr, arm, state, wait, resolution
            )
            if resolving is not None:
                return resolving

        if gate.outcome == "merge":
            if state.queued and state.phase == "merging":
                return await self._in_queue(ctx, profile, info, pr, arm, state, wait)
            rule = self._forge_blocks(pr, profile, info)
            if rule:
                return await self._held_by_forge(
                    ctx, profile, info, pr, arm, state, rule
                )
            subject, body = squash_message(pr, profile)
            methods = info.merge_methods
            method = (
                profile.merge.method
                if not methods or profile.merge.method in methods
                else methods[0]
            )
            queue = bool(info.merge_queue)
            action = step(
                ActionKind.PR_MERGE,
                MergePayload(
                    method=method,
                    subject=subject,
                    body=body,
                    armed_by=arm.armed_by,
                    queue=queue,
                ),
                f"merge:{state.merge_attempts}" if state.merge_attempts else "merge",
                "the merge gate holds",
            )
            state.queued = queue
            await self._set(
                ctx,
                profile,
                info,
                pr,
                state,
                "merging",
                "joining GitHub's merge queue" if queue else "merging",
            )
            return PlaybookResult(
                actions=[action],
                wait=wait.model_copy(update={"action_ids": [action.id]}),
                result={"phase": "merging"},
            )

        landing = (
            state.since is not None
            and (now - state.since).total_seconds() < LANDING_SECONDS
        )
        if one and pr.behind_base:
            if (
                state.phase == "updating"
                and same_commit(state.updated_from, pr.head_sha)
                and landing
            ):
                # The base merge was asked for and has not reached the PR yet.
                return PlaybookResult(
                    wait=wait.model_copy(
                        update={
                            "seconds": LANDING_SECONDS,
                            "reason": f"merging {pr.base_ref} in",
                        }
                    ),
                    result={"phase": "updating"},
                )
            if (
                pr.head_repo not in (None, "", pr.repo)
                and pr.maintainer_can_modify is False
            ):
                return await self._block(
                    ctx,
                    profile,
                    pr,
                    arm,
                    state,
                    f"it must merge {pr.base_ref} in, but its author does not let "
                    "maintainers push to its branch: ask them to merge "
                    f"{pr.base_ref} in, or to allow edits by maintainers",
                    False,
                )
            if not await self._signed_in(ctx, arm.armed_by):
                # A base merge is a commit on the PR's branch, made only as the
                # maintainer who scheduled it (``pipeline.ONLY_THE_SCHEDULER``).
                return await self._block(
                    ctx,
                    profile,
                    pr,
                    arm,
                    state,
                    f"it must merge {pr.base_ref} in, which the schedule does as "
                    f"{arm.armed_by}, who has not signed in to the dashboard with "
                    "GitHub: once they do (the account menu's Sign in with GitHub to "
                    "act as you), it goes on",
                    "scheduler",
                )
            if state.updates >= UPDATES_PER_TURN:
                return await self._fail(
                    ctx,
                    profile,
                    pr,
                    arm,
                    state,
                    f"{pr.base_ref} kept moving: it merged {pr.base_ref} "
                    f"{state.updates} times this turn",
                )
            state.updates += 1
            state.updated_from = pr.head_sha
            action = step(
                ActionKind.PR_UPDATE_BRANCH,
                UpdateBranchPayload(),
                # A retry after the forge refused one is an action of its own.
                f"update:{state.updates}"
                + (f":a{state.merge_attempts}" if state.merge_attempts else ""),
                f"behind {pr.base_ref}, which it must be up to date with",
            )
            await self._set(
                ctx, profile, info, pr, state, "updating", f"merging {pr.base_ref} in"
            )
            return PlaybookResult(
                actions=[action], wait=wait, result={"phase": "updating"}
            )

        stopped = [
            c
            for c in latest_checks(pr.checks)
            if c.run_id in set(state.cancelled)
            and c.conclusion == CheckConclusion.CANCELLED
        ]
        state.cancelled = []
        if stopped:
            # Cancelled while another PR held the turn: run again, past the budget.
            targets = rerun_targets(stopped)
            state.rerun_of = [c.id for c in stopped]
            await self._set(
                ctx,
                profile,
                info,
                pr,
                state,
                "rerunning",
                "running again the CI the schedule cancelled while another PR "
                "held the turn",
            )
            return PlaybookResult(
                actions=[
                    step(
                        ActionKind.CI_RERUN_FAILED,
                        RerunPayload(
                            run_id=t.run_id,
                            workflow=t.workflow,
                            attempt=t.attempt,
                            requested=True,
                        ),
                        f"resume:{t.run_id}:{t.attempt or 1}",
                        "the schedule cancelled it while another PR held the turn",
                    )
                    for t in targets
                ],
                wait=wait,
                result={"phase": "rerunning"},
            )
        if state.cancel_to_rerun:
            unfinished_now = unfinished_runs(pr.checks)
            still_stopping = [
                rid for rid in state.cancel_to_rerun if rid in unfinished_now
            ]
            ready_to_rerun = [
                rid for rid in state.cancel_to_rerun if rid not in unfinished_now
            ]
            if ready_to_rerun:
                ready_set = set(ready_to_rerun)
                latest = latest_checks(pr.checks)
                targets = [t for t in rerun_targets(latest) if t.run_id in ready_set]
                again: list[Action] = []
                rerun_run_ids: set[int] = set()
                for target in targets:
                    if state.retry:
                        rerun_run_ids.add(target.run_id)
                        again.append(
                            step(
                                ActionKind.CI_RERUN_FAILED,
                                RerunPayload(
                                    run_id=target.run_id,
                                    workflow=target.workflow,
                                    attempt=target.attempt,
                                    requested=True,
                                ),
                                f"retry:{target.run_id}:{state.retries}",
                                "rerunning after cancelling running CI as a maintainer asked",
                            )
                        )
                        continue
                    done = await reruns_at_head(ctx, pr, target.run_id)
                    if done < profile.ci.rerun.max_per_sha:
                        rerun_run_ids.add(target.run_id)
                        again.append(
                            step(
                                ActionKind.CI_RERUN_FAILED,
                                RerunPayload(
                                    run_id=target.run_id,
                                    workflow=target.workflow,
                                    attempt=target.attempt,
                                ),
                                f"rerun:{target.run_id}:{done}",
                                "rerunning after cancelling running CI on a flaky failure",
                            )
                        )
                state.cancel_to_rerun = still_stopping
                if not still_stopping:
                    state.retry = False
                if again:
                    handled_ids = [
                        c.id
                        for c in latest
                        if c.run_id in rerun_run_ids
                        and (c.failed or c.conclusion == CheckConclusion.CANCELLED)
                    ]
                    state.rerun_of = list(
                        dict.fromkeys([*state.rerun_of, *handled_ids])
                    )[-200:]
                    await self._set(
                        ctx,
                        profile,
                        info,
                        pr,
                        state,
                        "rerunning",
                        "rerunning CI after cancelling on a flaky failure",
                    )
                    return PlaybookResult(
                        actions=again,
                        wait=wait.model_copy(
                            update={
                                "seconds": CANCEL_POLL_SECONDS
                                if still_stopping
                                else WAIT_SECONDS,
                                "reason": "rerunning after flaky cancel",
                            }
                        ),
                        result={"phase": "rerunning"},
                    )
                if still_stopping and landing:
                    await self._note(
                        ctx,
                        pr,
                        state,
                        "rerunning",
                        state.detail or "waiting for cancelled CI to stop",
                    )
                    return PlaybookResult(
                        wait=wait.model_copy(
                            update={
                                "seconds": CANCEL_POLL_SECONDS,
                                "reason": "waiting for cancelled CI to stop",
                            }
                        ),
                        result={"phase": "rerunning"},
                    )
            elif still_stopping and landing:
                return PlaybookResult(
                    wait=wait.model_copy(
                        update={
                            "seconds": CANCEL_POLL_SECONDS,
                            "reason": "waiting for cancelled CI to stop",
                        }
                    ),
                    result={"phase": "rerunning"},
                )
            elif still_stopping:
                # Cancel did not land within LANDING_SECONDS: clear and reassess.
                state.cancel_to_rerun = []
                state.retry = False
        view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
        start = profile.ci.start
        if view.state == "gated" and view.label:
            action = step(
                ActionKind.LABEL_CI_GATE,
                LabelPayload(roles=["ci_gate"]),
                f"gate:{pr.head_sha[:12]}",
                f"its CI starts on the {view.label!r} label",
            )
            self._started(state, pr, now)
            await self._set(
                ctx, profile, info, pr, state, "starting", f"adding {view.label!r}"
            )
            return PlaybookResult(
                actions=[action],
                wait=wait.model_copy(update={"seconds": start.start_minutes * 60}),
                result={"phase": "starting"},
            )
        if view.state == "approval":
            self._started(state, pr, now)
            runs = sorted({c.run_id for c in view.held if c.run_id})
            workflows = sorted({c.workflow or c.name for c in view.held})
            # Proposed to the scheduler either way; the pipeline approves it as them
            # only on an arm they made by hand at code they saw, so otherwise it
            # waits for them to approve running a fork's code as themselves.
            action = step(
                ActionKind.CI_APPROVE_RUN,
                ApproveRunsPayload(run_ids=runs, workflows=workflows),
                "approve:" + ",".join(str(r) for r in runs),
                "GitHub holds its workflows for a maintainer",
            )
            if arm.auto or arm.unseen:
                state.approval_needed = True
                detail = (
                    "GitHub holds "
                    + ", ".join(workflows)
                    + f" until {arm.armed_by} approves running them"
                )
            else:
                detail = "approving " + ", ".join(workflows)
            await self._set(ctx, profile, info, pr, state, "starting", detail)
            return PlaybookResult(
                actions=[action],
                wait=wait.model_copy(update={"seconds": start.start_minutes * 60}),
                result={"phase": "starting"},
            )
        if view.state == "failed":
            failing = {c.id for c in failing_checks(pr, profile)} | {
                c.id for c in view.cancelled
            }
            if (
                state.phase == "rerunning"
                and failing <= set(state.rerun_of)
                and landing
            ):
                # The rerun was asked for and its new attempt has not started yet.
                return PlaybookResult(
                    wait=wait.model_copy(
                        update={"seconds": LANDING_SECONDS, "reason": "rerunning"}
                    ),
                    result={"phase": "rerunning"},
                )
            return await self._failed_ci(ctx, profile, info, pr, arm, state, wait, step)
        pending = [i for i in gate.items if not i.ok and i.pending]
        waiting = left_for_merge(gate, ready_conditions(profile, arm))
        if view.state == "passed" and waiting and not pending:
            # CI passed; what the merge needs beyond the repository's target to
            # schedule (approvals, where reviewers approve after CI) is missing.
            text = _waiting_for(waiting)
            events = await self._set(
                ctx, profile, info, pr, state, "approval", text, wake=True
            )
            return PlaybookResult(
                actions=[
                    self._tell(
                        pr,
                        arm,
                        "merge.approval",
                        f"CI passed at {short_sha(pr.head_sha)}; it merges once "
                        f"it meets: {text}",
                    )
                ],
                events=events,
                wait=wait.model_copy(update={"reason": "waiting for approvals"}),
                result={"phase": "approval"},
            )
        if view.state == "running":
            # Diagnose any failed workflow job immediately without waiting for the
            # rest of the workflow (or other long-tail jobs) to finish: flakes cancel
            # the still-running workflow and rerun it; real failures surface at once
            # and alert the scheduler.
            failed_now = [
                c
                for c in failing_checks(pr, profile)
                if c.run_id
                and c.id not in state.rerun_of
                and c.run_id not in state.cancel_to_rerun
            ]
            if failed_now:
                return await self._failed_ci(
                    ctx, profile, info, pr, arm, state, wait, step, checks=failed_now
                )
            asked = {c.id for c in failing_checks(pr, profile)} & set(state.rerun_of)
            if state.phase == "rerunning" and asked and landing:
                # A rerun was asked for while the rest runs and has not started yet.
                return PlaybookResult(
                    wait=wait.model_copy(
                        update={"seconds": LANDING_SECONDS, "reason": "rerunning"}
                    ),
                    result={"phase": "rerunning"},
                )
            state.running_since = state.running_since or now
            limit, since = start.run_minutes, state.running_since
            phase, what = "running", "has been running"
        else:
            # Nothing ran, the newest run stopped at its gate, or CI passed while a
            # required check the gate waits for has not reported.
            self._started(state, pr, now)
            limit, since = start.start_minutes, state.started_at or now
            phase, what = (
                "starting",
                (
                    "passed except "
                    + ", ".join(i.name.split(":", 1)[-1] for i in pending)
                    + ", which never reported"
                    if view.state == "passed" and pending
                    else "has not started"
                ),
            )
        minutes = (now - since).total_seconds() / 60
        actions = []
        skipped = await self._never_reported(ctx, pr, info, state, pending)
        if skipped:
            # A required check whose workflow skips this PR: no wait brings it.
            if not state.finding:
                state.finding = skipped
                actions = [self._tell(pr, arm, "merge.stalled", skipped)]
        elif minutes >= limit:
            actions = await self._look_into(
                ctx, profile, pr, arm, state, what, minutes, step
            )
        detail = state.finding or (
            f"{view.passed} passed, {view.running} running"
            if phase == "running"
            else what
        )
        await self._set(ctx, profile, info, pr, state, phase, detail)
        # Read again at the limit, and frequently while CI is running so mid-run
        # failures are caught promptly even without a webhook.
        poll_cap = RUNNING_POLL_SECONDS if phase == "running" else WAIT_SECONDS
        left = (
            max(60, min(poll_cap, int((limit - minutes) * 60)))
            if minutes < limit
            else poll_cap
        )
        return PlaybookResult(
            actions=actions,
            wait=wait.model_copy(update={"seconds": left, "reason": f"CI {what}"}),
            result={"phase": phase},
        )

    def _started(self, state: ScheduleState, pr: PrSnapshot, now: datetime) -> None:
        """Note when getting CI going began at this head."""
        if state.started_at is None or not same_commit(state.sha, pr.head_sha):
            state.started_at = now
            state.running_since = None
            state.finding = ""

    async def _failed_ci(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        wait: Wait,
        step: Any,
        checks: list | None = None,
    ) -> PlaybookResult:
        """Rerun what flaked within the budget; otherwise the failure lane.

        ``checks`` are the failures of workflow runs that ended while others still
        run, judged now; ``None`` for every failure once all of CI ended. A failed
        check of an outside service (no run here to rerun, such as Read the Docs)
        never holds back the reruns of the others: unless the PR caused it, it is
        judged once they end."""
        partial = checks is not None
        checks = checks if checks is not None else failing_checks(pr, profile)
        if state.retry:
            # A maintainer sent it back from the failure lane: run what failed
            # again, past the budget, before judging it anew. If a failed run still
            # has jobs in flight, cancel it first and rerun once it stops.
            view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
            runs = {c.run_id for c in [*checks, *view.cancelled] if c.run_id}
            unfinished_now = unfinished_runs(pr.checks)
            running_to_cancel = {
                rid: unfinished_now[rid] for rid in runs if rid in unfinished_now
            }
            targets = [
                t
                for t in rerun_targets(latest_checks(pr.checks))
                if t.run_id in runs and t.run_id not in running_to_cancel
            ]
            if targets or running_to_cancel:
                actions_retry: list[Action] = [
                    step(
                        ActionKind.CI_RERUN_FAILED,
                        RerunPayload(
                            run_id=t.run_id,
                            workflow=t.workflow,
                            attempt=t.attempt,
                            requested=True,
                        ),
                        f"retry:{t.run_id}:{state.retries}",
                        "a maintainer sent it back to run again",
                    )
                    for t in targets
                ]
                if running_to_cancel:
                    state.cancel_to_rerun = sorted(
                        {*state.cancel_to_rerun, *running_to_cancel}
                    )
                    actions_retry.append(
                        cancel_runs_action(
                            pr,
                            running_to_cancel,
                            "cancelling running CI to rerun as a maintainer asked",
                            by=arm.armed_by,
                            scheduled=int(arm.armed_at.timestamp()),
                            tag=f"retry:{state.retries}",
                        )
                    )
                else:
                    state.retry = False
                state.rerun_of = list(
                    dict.fromkeys([c.id for c in [*checks, *view.cancelled]])
                )
                await self._set(
                    ctx,
                    profile,
                    info,
                    pr,
                    state,
                    "rerunning",
                    "cancelling running CI to rerun, as a maintainer asked"
                    if running_to_cancel
                    else "running what failed again, as a maintainer asked",
                )
                return PlaybookResult(
                    actions=actions_retry,
                    wait=wait.model_copy(
                        update={
                            "seconds": CANCEL_POLL_SECONDS,
                            "reason": "cancelling running CI to retry",
                        }
                    )
                    if running_to_cancel
                    else wait,
                    result={"phase": "rerunning", "retry": state.retries},
                )
            state.retry = False
        if not checks:
            # Only cancelled runs: nothing failed, the run stopped short.
            view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
            again = []
            for target in rerun_targets(view.cancelled):
                done = await reruns_at_head(ctx, pr, target.run_id)
                if done < profile.ci.rerun.max_per_sha:
                    again.append(
                        step(
                            ActionKind.CI_RERUN_FAILED,
                            RerunPayload(
                                run_id=target.run_id,
                                workflow=target.workflow,
                                attempt=target.attempt,
                            ),
                            f"rerun:{target.run_id}:{done}",
                            "its run was cancelled",
                        )
                    )
            if not again:
                return await self._fail(
                    ctx, profile, pr, arm, state, "its CI was cancelled, again", []
                )
            state.rerun_of = [c.id for c in view.cancelled]
            await self._set(
                ctx, profile, info, pr, state, "rerunning", "rerunning a cancelled run"
            )
            return PlaybookResult(
                actions=again, wait=wait, result={"phase": "rerunning"}
            )
        await self._note(
            ctx,
            pr,
            state,
            "diagnosing",
            "looking at the failed jobs: " + ", ".join(c.name for c in checks),
        )
        failures = await first_looks(ctx, pr, profile, checks)
        unfinished = unfinished_runs(pr.checks)
        reruns: list[Action] = []
        to_cancel: dict[int, str] = {}
        cancel_done: dict[int, int] = {}
        retrying: set[int] = set()
        rerun = profile.ci.rerun

        def queue_retry(f: Any, done: int, rationale: str) -> None:
            rid = f.check.run_id
            if not rid:
                return
            retrying.add(f.check.id)
            if rid in unfinished:
                to_cancel[rid] = unfinished[rid]
                cancel_done[rid] = min(cancel_done.get(rid, done), done)
            elif rid not in {a.payload["run_id"] for a in reruns}:
                reruns.append(
                    step(
                        ActionKind.CI_RERUN_FAILED,
                        RerunPayload(
                            run_id=rid,
                            workflow=f.check.workflow,
                            attempt=f.check.run_attempt,
                        ),
                        f"rerun:{rid}:{done}",
                        rationale,
                    )
                )

        for f in failures:
            if not f.check.run_id:
                continue
            flaky = f.diagnosis.category in ("flaky", "infra")
            # A flake gets the repository's reruns; a failure nothing calls a flake
            # gets ``ci.rerun.unknown`` of them first, before it counts as real.
            limit = (
                rerun.max_per_sha if flaky else min(rerun.unknown, rerun.max_per_sha)
            )
            if not limit:
                continue
            done = await reruns_at_head(ctx, pr, f.check.run_id)
            if done >= limit:
                f.reruns = done
                continue
            queue_retry(f, done, f"{f.check.name}: {f.diagnosis.explanation}")
        stuck = [f for f in failures if f.check.id not in retrying]
        found: tuple[list[RootCause] | None, str] | None = None
        by_job: dict[str, RootCause] = {}
        if stuck:
            # The first look could not call these flakes: the root cause decides
            # before the failure lane. One the runner or a flake caused is run again
            # within the flake budget; one in the change, the base, or unclear is not.
            real = [f.check.name for f in failures if f.real]
            if real and profile.ci.root_cause:
                await self._note(
                    ctx,
                    pr,
                    state,
                    "diagnosing",
                    "looking for the root cause of " + ", ".join(real),
                )
            found = await find_causes(ctx, pr, profile, failures)
            by_job = {job: c for c in found[0] or [] for job in c.jobs}
            for f in stuck:
                cause = by_job.get(f.check.name)
                if cause is None or cause.origin not in ("infra", "flaky"):
                    continue
                f.diagnosis = f.diagnosis.model_copy(
                    update={"category": cause.origin, "explanation": cause.cause}
                )
                done = await reruns_at_head(ctx, pr, f.check.run_id or 0)
                if not f.check.run_id or done >= rerun.max_per_sha:
                    f.reruns = done
                    continue
                f.reruns = 0
                queue_retry(f, done, f"{f.check.name}: {cause.cause}")
            stuck = [f for f in failures if f.check.id not in retrying]

        def caused_here(f: Any) -> bool:
            cause = by_job.get(f.check.name)
            if cause is not None:
                return cause.origin in ("change", "base")
            return f.diagnosis.category == "pr_caused"

        # An outside service's failed check (no run here to rerun) that the PR did
        # not cause waits for the reruns of the others, and is judged after them.
        later = [f for f in stuck if not f.check.run_id and not caused_here(f)]
        if len(later) < len(stuck) or (not reruns and not to_cancel):
            jobs = ", ".join(f.check.name for f in stuck or failures) or "CI"
            return await self._fail(
                ctx, profile, pr, arm, state, f"{jobs} failed", failures, found
            )
        await record(ctx, pr, failures, found[0] if found else None)
        handled = [f.check.id for f in failures if f.check.id in retrying] + [
            f.check.id for f in later
        ]
        # Until their new attempts replace them, these are not failures to act on
        # again (a rerun asked for twice would run twice).
        state.rerun_of = [*state.rerun_of, *handled][-200:] if partial else handled
        names = ", ".join(f.check.name for f in failures if f.check.id in retrying)
        actions: list[Action] = [*reruns]
        if to_cancel:
            state.cancel_to_rerun = sorted({*state.cancel_to_rerun, *to_cancel})
            tag = f"flaky:{min(cancel_done.values())}"
            actions.append(
                cancel_runs_action(
                    pr,
                    to_cancel,
                    f"cancelling running CI to rerun flaky {names}",
                    by=arm.armed_by,
                    scheduled=int(arm.armed_at.timestamp()),
                    tag=tag,
                )
            )
            detail = f"cancelling running CI to rerun {names} (flaky)"
        else:
            detail = f"rerunning {names}"
            if later:
                detail += (
                    "; " + ", ".join(f.check.name for f in later) + " cannot be rerun "
                    "from here and is judged once the reruns end"
                )
            elif partial:
                detail += " while the rest of CI runs"
        await self._set(ctx, profile, info, pr, state, "rerunning", detail)
        next_wait = (
            wait.model_copy(
                update={
                    "seconds": CANCEL_POLL_SECONDS,
                    "reason": "cancelling running CI to rerun flaky failure",
                }
            )
            if to_cancel
            else wait
        )
        return PlaybookResult(
            actions=actions, wait=next_wait, result={"phase": "rerunning"}
        )

    async def _look_into(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        what: str,
        minutes: float,
        step: Any,
    ) -> list[Action]:
        """One quick look per head and kind of stall at why CI is stuck, and the step
        it names when the scheduler may take it."""
        mark = f"{pr.head_sha[:12]}:{what.split(' ')[0]}"
        if mark in state.looked_into:
            return []
        state.looked_into = [*state.looked_into[-9:], mark]
        runs = "\n".join(
            f"- {c.workflow or '(no workflow)'} / {c.name}: {c.status}"
            + (f", {c.conclusion}" if c.conclusion else "")
            + (f" (run {c.run_id}, attempt {c.run_attempt or 1})" if c.run_id else "")
            + ("" if c in latest_checks(pr.checks) else " [superseded]")
            for c in pr.checks[:120]
        )
        info = await ctx.forge.get_repo(pr.repo)
        facts = "\n".join(
            [
                f"Labels: {', '.join(pr.labels) or '(none)'}",
                f"Gate label: {profile.label_for('ci_gate') or '(none)'}",
                f"Required checks: {', '.join(info.required_checks or []) or '(none)'}",
                f"Fork approval policy: {info.fork_approval or 'unknown'}",
                f"The PR comes from a fork: {bool(pr.head_repo and pr.head_repo != pr.repo)}",
            ]
        )
        prompt = "\n\n".join(
            part
            for part in [
                STALLED.format(
                    number=pr.number,
                    repo=pr.repo,
                    sha=short_sha(pr.head_sha),
                    what=what,
                    minutes=int(minutes),
                ),
                facts,
                "How this repository's CI starts, as its maintainers wrote it:\n"
                + untrusted("ci.start", profile.ci.start.how, 4_000)
                if profile.ci.start.how.strip()
                else "",
                untrusted("ci.checks", runs or "(no checks at this head)", 12_000),
                schema_hint(StallFinding.model_json_schema()),
            ]
            if part
        )
        await self._note(ctx, pr, state, state.phase, f"looking into why CI {what}")
        try:
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.CI_DIAGNOSE,
                    prompt=prompt,
                    workdir=ctx.scratch(),
                    mode="read_only",
                    budget="quick",
                    output_schema=StallFinding.model_json_schema(),
                    repo=pr.repo,
                    pr=pr.number,
                    untrusted_input=True,
                    activity="ci_stall",
                ),
                StallFinding,
            )
        except Exception:  # the look costs the schedule only its finding
            return []
        if not answer.ok or answer.structured is None:
            return []
        found = StallFinding.model_validate(answer.structured)
        state.finding = found.cause + (
            f" {found.for_maintainer}" if found.for_maintainer else ""
        )
        latest = latest_checks(pr.checks)
        gate_label = profile.label_for("ci_gate")
        if found.step == "add_label" and gate_label and gate_label not in pr.labels:
            return [
                step(
                    ActionKind.LABEL_CI_GATE,
                    LabelPayload(roles=["ci_gate"]),
                    f"gate:{pr.head_sha[:12]}:look",
                    found.cause,
                )
            ]
        if found.step == "approve_runs":
            held = [c for c in latest if c.awaiting_approval and c.run_id]
            if held:
                return [
                    step(
                        ActionKind.CI_APPROVE_RUN,
                        ApproveRunsPayload(
                            run_ids=sorted({c.run_id for c in held if c.run_id}),
                            workflows=sorted({c.workflow or c.name for c in held}),
                        ),
                        f"approve:{pr.head_sha[:12]}:look",
                        found.cause,
                    )
                ]
        if found.step == "rerun" and found.run_id:
            here = [c for c in latest if c.run_id == found.run_id]
            done = await reruns_at_head(ctx, pr, found.run_id)
            if here and done < profile.ci.rerun.max_per_sha:
                return [
                    step(
                        ActionKind.CI_RERUN_FAILED,
                        RerunPayload(run_id=found.run_id, workflow=here[0].workflow),
                        f"rerun:{found.run_id}:{done}",
                        found.cause,
                    )
                ]
        if found.step in ("maintainer", "approve_runs", "add_label", "rerun"):
            return [self._tell(pr, arm, "merge.stalled", state.finding)]
        return []

    def _tell(self, pr: PrSnapshot, arm: MergeArm, event: str, text: str) -> Action:
        return make_action(
            ActionKind.NOTIFY,
            pr.repo,
            NotifyPayload(
                event=event,
                text=f"{pr.repo}#{pr.number} (scheduled by {arm.armed_by}): {text}",
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=f"{event}:{text[:40]}",
        )

    async def _fail(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        why: str,
        failures: list | None = None,
        found: tuple[list[RootCause] | None, str] | None = None,
    ) -> PlaybookResult:
        """Move the PR to the failure lane, with the root cause of what failed
        (``found`` when it was looked for already), and pass the turn on."""
        actions: list[Action] = []
        state.failed_on_ci = failures is not None
        state.failed_jobs = [f.check.name for f in failures or [] if f.real]
        state.failed_ids = [f.check.id for f in failures or []]
        state.causes = []
        if failures:
            if found is None:
                real = [f.check.name for f in failures if f.real]
                if real and profile.ci.root_cause:
                    await self._note(
                        ctx,
                        pr,
                        state,
                        "diagnosing",
                        "looking for the root cause of " + ", ".join(real),
                    )
                found = await find_causes(ctx, pr, profile, failures)
            causes, base = found
            await record(ctx, pr, failures, causes)
            state.causes = explained(failures, causes, base)
            fixing = await self._start_fix(
                ctx, profile, pr, arm, state, failures, causes
            )
            if state.causes:
                actions.append(
                    make_action(
                        ActionKind.COMMENT_UPSERT,
                        pr.repo,
                        CommentUpsertPayload(
                            purpose="ci",
                            body=f"### CI failures at `{short_sha(pr.head_sha)}`\n\n"
                            + "\n".join(state.causes),
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator="schedule:"
                        + ",".join(sorted(str(f.check.id) for f in failures)),
                        hints=ActionHints(
                            author_is_team=await ctx.author_is_team(pr),
                            changed_paths=pr.paths,
                        ),
                    )
                )
            if fixing is not None:
                return fixing.model_copy(
                    update={"actions": [*actions, *fixing.actions]}
                )
        return await self._lane(ctx, profile, pr, arm, state, why, actions)

    async def _lane(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        why: str,
        actions: list[Action] | None = None,
    ) -> PlaybookResult:
        """Put the PR in the failure lane, with what ``state`` says failed, remind
        the scheduler to fix it, and pass the turn on."""
        state.cancel_to_rerun = []
        alert = f"{why} — @{arm.armed_by}, please fix or intervene"
        if state.causes:
            alert += "\n" + "\n".join(state.causes[:3])
        actions = [*(actions or []), self._tell(pr, arm, "merge.failed", alert)]
        info = await ctx.forge.get_repo(pr.repo)
        events = await self._set(
            ctx, profile, info, pr, state, "failed", why, wake=True
        )
        actions += await self._quiet(
            ctx, profile, info, pr, arm, state, _numbers(events)
        )
        return PlaybookResult(
            actions=actions,
            events=events,
            wait=Wait(
                kinds=WAIT_KINDS, seconds=WAIT_SECONDS, reason="in the failure lane"
            ),
            result={"phase": "failed", "why": why},
        )

    async def _quiet(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        taking: list[int] | None = None,
    ) -> list[Action]:
        """Where scheduled PRs take turns (``serial``), the step that cancels this
        PR's unfinished workflow runs while another holds the turn (or takes it now,
        ``taking``): once that one merges, they test a base that is gone, and they
        hold CI machines it needs. The runs go into ``state.cancelled``, so its own
        turn runs them again."""
        if not serial(info, profile):
            return []
        runs = unfinished_runs(pr.checks)
        if not runs:
            return []
        holders = [n for n in taking or [] if n != pr.number]
        if not holders:
            for other in ordered(await ctx.store.list_arms(pr.repo)):
                if other.number == pr.number:
                    continue
                if (await load_state(ctx, pr.repo, other.number)).phase in ACTIVE:
                    holders.append(other.number)
        if not holders:
            return []
        state.cancelled = sorted({*state.cancelled, *runs})
        await ctx.store.set_state(
            state_key(pr.repo, pr.number), state.model_dump(mode="json")
        )
        return [
            cancel_runs_action(
                pr,
                runs,
                f"#{holders[0]} holds the merge schedule's turn, and the base moves "
                "when it merges",
                by=arm.armed_by,
                scheduled=int(arm.armed_at.timestamp()),
            )
        ]

    async def _to_bottom(self, ctx: PlaybookContext, arm: MergeArm) -> MergeArm:
        """``arm`` moved after every other PR in its repository's schedule."""
        others = [
            a for a in await ctx.store.list_arms(arm.repo) if a.number != arm.number
        ]
        moved = arm.model_copy(
            update={"position": next_position(others, ctx.clock.now())}
        )
        await ctx.store.save_arm(moved)
        return moved

    async def _start_fix(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        failures: list,
        causes: list[RootCause] | None,
    ) -> PlaybookResult | None:
        """Start the schedule's own fix (``merge.schedule.fix``, by
        ``playbooks/fix_ci``, for the scheduler) when every real failure is a static
        check's finding, keeping the turn while it runs; ``None`` when it does not
        fix this one: something else fails, its author lets no maintainer push to it,
        a fix was tried at this head, or ``FIXES_PER_SCHEDULE`` were pushed."""
        if not profile.merge.schedule.fix.enabled:
            return None
        real = sorted({f.check.name for f in failures if f.real})
        by_job = {job: c for c in causes or [] for job in c.jobs}
        if not real or any(
            (c := by_job.get(name)) is None or not c.static or c.origin == "infra"
            for name in real
        ):
            return None
        if (
            pr.head_repo not in (None, "", pr.repo)
            and pr.maintainer_can_modify is False
        ):
            return None
        tried = await ctx.store.get_state(fix_key(pr.repo, pr.number)) or {}
        if same_commit(tried.get("sha"), pr.head_sha):
            return None
        pushed = [
            r
            for r in await ctx.store.list_actions(
                repo=pr.repo,
                number=pr.number,
                kinds=[ActionKind.PR_FIX],
                since=arm.armed_at,
            )
            if r.action.payload.get("schedule_fix")
        ]
        if len(pushed) >= FIXES_PER_SCHEDULE:
            return None
        await self._fix_noted(ctx, pr, "started")
        info = await ctx.forge.get_repo(pr.repo)
        await self._set(
            ctx,
            profile,
            info,
            pr,
            state,
            "fixing",
            "an agent fixes what " + ", ".join(real) + " reports",
        )
        return PlaybookResult(
            events=[
                Event(
                    kind=EventKind.DASHBOARD_COMMAND,
                    repo=pr.repo,
                    number=pr.number,
                    payload={
                        "command": "fix_ci",
                        "by": arm.armed_by,
                        "jobs": real,
                        "schedule": True,
                    },
                )
            ],
            wait=Wait(
                kinds=WAIT_KINDS, seconds=FIX_SECONDS, reason="fixing its static checks"
            ),
            result={"phase": "fixing", "jobs": real},
        )

    async def _fix_noted(
        self, ctx: PlaybookContext, pr: PrSnapshot, outcome: str, why: str = ""
    ) -> None:
        await ctx.store.set_state(
            fix_key(pr.repo, pr.number),
            {
                "sha": pr.head_sha,
                "outcome": outcome,
                "why": why,
                "at": ctx.clock.now().isoformat(),
            },
        )

    async def _is_fix(self, ctx: PlaybookContext, event: Event) -> bool:
        record_ = await ctx.store.get_action(str(event.payload.get("action_id") or ""))
        return bool(record_ and record_.action.payload.get("schedule_fix"))

    async def _fixing(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        wait: Wait,
    ) -> PlaybookResult:
        """Follow the schedule's fix of the PR's static checks at this head until its
        push shows (a new head, which the schedule keeps as its scheduler's own
        push), or send the PR to the failure lane with why it was not fixed."""
        tried = await ctx.store.get_state(fix_key(pr.repo, pr.number)) or {}
        if not same_commit(tried.get("sha"), pr.head_sha):
            tried = {}
        outcome = tried.get("outcome")
        jobs = ", ".join(state.failed_jobs) or "its static checks"
        if outcome not in ("started", "proposed"):
            why = tried.get("why") or "it was not kept"
            return await self._lane(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"{jobs} failed, and the schedule did not fix it: {why}",
            )
        now = ctx.clock.now()
        began = datetime.fromisoformat(str(tried["at"]))
        detail = f"an agent fixes what {jobs} reports"
        pending = False
        if outcome == "proposed":
            record_ = await ctx.store.get_action(str(tried.get("action_id") or ""))
            status = record_.status if record_ else None
            if status in (ActionStatus.SHADOWED, ActionStatus.DENIED, None):
                rule = record_.decision.rule if record_ and record_.decision else ""
                return await self._lane(
                    ctx,
                    profile,
                    pr,
                    arm,
                    state,
                    f"{jobs} failed; the schedule fixed it, but the repository's "
                    f"pr.fix rule did not let it push the fix ({rule or 'not kept'})",
                )
            if status == ActionStatus.PROPOSED:
                pending = True
                detail = (
                    f"its fix of {jobs} waits for {arm.armed_by} to approve it under "
                    "Approvals, or to sign in to the dashboard so the schedule pushes "
                    "it as them"
                )
            else:
                detail = f"pushing its fix of {jobs}"
        spent = (now - began).total_seconds()
        if spent >= FIX_SECONDS and not pending:
            return await self._lane(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"{jobs} failed, and the schedule's fix did not land within "
                f"{FIX_SECONDS // 60} minutes",
            )
        await self._set(ctx, profile, info, pr, state, "fixing", detail)
        return PlaybookResult(
            wait=wait.model_copy(
                update={
                    "seconds": WAIT_SECONDS
                    if pending
                    else max(60, int(FIX_SECONDS - spent)),
                    "reason": "fixing its static checks",
                }
            ),
            result={"phase": "fixing"},
        )

    async def _note(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        state: ScheduleState,
        phase: str,
        detail: str,
    ) -> None:
        """Show what the PR's job is doing before a slow step (an agent at work),
        without handing the turn on: the page shows it with the job's live view."""
        if state.phase != phase:
            state.since = ctx.clock.now()
        state.phase = phase  # type: ignore[assignment]
        state.sha = pr.head_sha
        state.detail = detail
        await ctx.store.set_state(
            state_key(pr.repo, pr.number), state.model_dump(mode="json")
        )

    async def _set(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        state: ScheduleState,
        phase: str,
        detail: str,
        wake: bool = False,
    ) -> list[Event]:
        """Keep the PR's phase; when it gave up its turn (``wake``), the events that
        wake the PRs whose turn it is now."""
        was_active = state.phase in ACTIVE
        if state.phase != phase or not same_commit(state.sha, pr.head_sha):
            state.since = ctx.clock.now()
            # What an agent found about a stall belongs to the phase it stalled in.
            state.finding = ""
        state.phase = phase  # type: ignore[assignment]
        state.sha = pr.head_sha
        state.detail = detail
        if phase != "starting":
            state.approval_needed = state.approval_needed and phase == "starting"
        await ctx.store.set_state(
            state_key(pr.repo, pr.number), state.model_dump(mode="json")
        )
        if wake and (was_active or phase in ("failed", "held", "approval", "blocked")):
            return await self._wake(ctx, profile, info, pr.repo, pr.number)
        return []

    async def _wake(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        repo: str,
        leaving: int,
    ) -> list[Event]:
        """``merge.queue`` events for the waiting PRs whose turn it is now."""
        arms = ordered(await ctx.store.list_arms(repo))
        order = [a.number for a in arms]
        states = {a.number: await load_state(ctx, repo, a.number) for a in arms}
        allowed = turns(
            order, states, serial(info, profile), profile.merge.schedule.parallel
        )
        return [
            Event(
                kind=EventKind.MERGE_QUEUE,
                repo=repo,
                number=n,
                payload={"after": leaving},
            )
            for n in allowed
            if n != leaving and states[n].phase in WAITING
        ]

    async def _leave(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        repo: str,
        number: int,
        merged: bool = False,
        pr: PrSnapshot | None = None,
    ) -> PlaybookResult:
        """The PR left the schedule (merged, closed, or unscheduled): forget its
        phase, list it under Merged lately when it merged, and pass its turn on."""
        state = await load_state(ctx, repo, number)
        await ctx.store.set_state(state_key(repo, number), None)
        if merged:
            key = MERGED_KEY.format(repo=repo)
            kept = await ctx.store.get_state(key) or []
            snap = pr or await ctx.store.get_snapshot(repo, number)
            kept = [
                {
                    "number": number,
                    "title": snap.title if snap else "",
                    "at": ctx.clock.now().isoformat(),
                    "since": state.since.isoformat() if state.since else None,
                },
                *(k for k in kept if k.get("number") != number),
            ][:MERGED_KEPT]
            await ctx.store.set_state(key, kept)
        info = await ctx.forge.get_repo(repo)
        events = await self._wake(ctx, profile, info, repo, number)
        return PlaybookResult(
            events=events, result={"done": "merged" if merged else "left the schedule"}
        )

    async def _signed_in(self, ctx: PlaybookContext, login: str) -> bool:
        """Whether ``login`` signed in so the schedule can act as them."""
        check = ctx.service.can_post_as
        return check is None or await check(login)

    async def _block(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        why: str,
        scope: bool | str,
    ) -> PlaybookResult:
        """Keep the PR in the order, blocked, after the forge refused its merge or
        base merge, and pass the turn on: it tries again once a maintainer retries
        it, or after the service starts again (a new sign-in) when a scope was
        missing, or when the PR changes otherwise."""
        # ``scope``: whose sign-in lacked a scope (``service``, ``person``), if any;
        # ``scheduler`` when a step only the scheduler takes waits for their sign-in.
        state.blocked_on = (
            "person_scope"
            if scope == "person"
            else "scheduler_sign_in"
            if scope == "scheduler"
            else "workflow_scope"
            if scope
            else "refused"
        )
        state.merge_attempts += 1
        info = await ctx.forge.get_repo(pr.repo)
        events = await self._set(
            ctx, profile, info, pr, state, "blocked", why, wake=True
        )
        if state.blocked_on == "workflow_scope":
            # The service asks its owner to add the scope to its sign-in.
            events.append(
                Event(
                    kind=EventKind.SCOPE_NEEDED,
                    repo=pr.repo,
                    payload={"scopes": ["workflow"], "number": pr.number},
                )
            )
        return PlaybookResult(
            actions=[self._tell(pr, arm, "merge.blocked", why)],
            events=events,
            wait=Wait(
                kinds=WAIT_KINDS, seconds=BLOCKED_SECONDS, reason="merge blocked"
            ),
            result={"phase": "blocked", "why": why},
        )

    async def _resolution(
        self, ctx: PlaybookContext, pr: PrSnapshot
    ) -> dict[str, Any] | None:
        """How the schedule's resolution of ``pr``'s conflicts went at its head
        (``merge_schedule.resolve_key``), or ``None``."""
        found = await ctx.store.get_state(resolve_key(pr.repo, pr.number))
        if isinstance(found, dict) and same_commit(found.get("sha"), pr.head_sha):
            return found
        return None

    async def _resolve(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        wait: Wait,
        resolution: dict[str, Any] | None,
    ) -> PlaybookResult | None:
        """On a conflicting PR's turn: start the schedule's resolution of its
        conflicts (``playbooks/merge_base``, for the scheduler), or follow it until
        its push shows; ``None`` when there is nothing to resolve any more."""
        now = ctx.clock.now()
        base = pr.base_ref
        outcome = (resolution or {}).get("outcome")
        if outcome == "clean":
            return None
        if outcome in ("started", "proposed"):
            began = datetime.fromisoformat(str(resolution["at"]))  # type: ignore[index]
            detail = f"an agent resolves its conflicts with {base}"
            pending = False
            if outcome == "proposed":
                record_ = await ctx.store.get_action(
                    str(resolution.get("action_id") or "")  # type: ignore[union-attr]
                )
                status = record_.status if record_ else None
                if status in (ActionStatus.SHADOWED, ActionStatus.DENIED, None):
                    rule = record_.decision.rule if record_ else "it was not kept"
                    return await self._fail(
                        ctx,
                        profile,
                        pr,
                        arm,
                        state,
                        f"it conflicts with {base}; the schedule resolved it, but the "
                        f"repository's pr.fix rule did not let it push the resolution "
                        f"({rule})",
                    )
                if status == ActionStatus.PROPOSED:
                    pending = True
                    detail = (
                        f"its conflicts with {base} are resolved; the push waits for "
                        f"{arm.armed_by} to approve it under Approvals, or to sign in "
                        "to the dashboard so the schedule pushes it as them"
                    )
                else:
                    detail = f"pushing its resolution of the conflicts with {base}"
            spent = (now - began).total_seconds()
            if spent >= RESOLVE_SECONDS and not pending:
                await ctx.store.set_state(resolve_key(pr.repo, pr.number), None)
                return await self._fail(
                    ctx,
                    profile,
                    pr,
                    arm,
                    state,
                    f"it conflicts with {base}, and the schedule's resolution did "
                    f"not land within {RESOLVE_SECONDS // 60} minutes; retry it, or "
                    "resolve the conflicts on the PR page",
                )
            await self._set(ctx, profile, info, pr, state, "resolving", detail)
            return PlaybookResult(
                wait=wait.model_copy(
                    update={
                        "seconds": WAIT_SECONDS
                        if pending
                        else max(60, int(RESOLVE_SECONDS - spent)),
                        "reason": "resolving conflicts",
                    }
                ),
                result={"phase": "resolving"},
            )
        if (
            pr.head_repo not in (None, "", pr.repo)
            and pr.maintainer_can_modify is False
        ):
            return await self._block(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"it conflicts with {base}, but its author does not let maintainers "
                f"push to its branch: ask them to merge {base} in, or to allow edits "
                "by maintainers",
                False,
            )
        if state.updates >= UPDATES_PER_TURN:
            return await self._fail(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"{base} kept moving: it merged {base} {state.updates} times this turn",
            )
        state.updates += 1
        state.updated_from = pr.head_sha
        await ctx.store.set_state(
            resolve_key(pr.repo, pr.number),
            {"sha": pr.head_sha, "outcome": "started", "at": now.isoformat()},
        )
        kinds = ", ".join(
            k.name for k in profile.merge.schedule.resolve.kinds if k.enabled
        )
        await self._set(
            ctx,
            profile,
            info,
            pr,
            state,
            "resolving",
            f"an agent resolves its conflicts with {base} (only {kinds})",
        )
        return PlaybookResult(
            events=[
                Event(
                    kind=EventKind.DASHBOARD_COMMAND,
                    repo=pr.repo,
                    number=pr.number,
                    payload={"command": RESOLVE_COMMAND, "by": arm.armed_by},
                )
            ],
            wait=wait.model_copy(
                update={"seconds": RESOLVE_SECONDS, "reason": "resolving conflicts"}
            ),
            result={"phase": "resolving"},
        )

    def _forge_blocks(
        self, pr: PrSnapshot, profile: RepoProfile, info: RepoInfo
    ) -> str:
        """Why GitHub's own verdict holds a merge the merge gate allows, or empty:
        a rule the gate does not read (a required deployment, signed commits, push
        restrictions). A failing check the repository lets stay unpassed explains a
        ``BLOCKED``, and so does a merge queue, which merges in its own time. When
        the service's account cannot merge past the branch's rules, nothing holds:
        GitHub refuses a merge a rule blocks and says which, and its ``BLOCKED`` can
        trail what it enforces."""
        if (
            pr.merge_state != "BLOCKED"
            or info.merge_queue
            or info.service_bypasses_rules is False
        ):
            return ""
        optional = {c.lower() for c in profile.ci.start.optional_checks}
        passing = {
            CheckConclusion.SUCCESS,
            CheckConclusion.NEUTRAL,
            CheckConclusion.SKIPPED,
        }
        if any(
            c.name.lower() in optional and c.conclusion not in passing
            for c in latest_checks(pr.checks)
        ):
            return ""
        return (
            "GitHub still reports its merge blocked, though the merge rules the "
            "service reads hold: a rule it does not read holds it (a required "
            "deployment, signed commits, push restrictions, a ruleset). It looks "
            "again every "
            f"{RULES_SECONDS // 60} minutes; a maintainer can merge it on GitHub"
        )

    async def _held_by_forge(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        why: str,
    ) -> PlaybookResult:
        """Hold a merge GitHub itself reports blocked, without trying it: the
        service's own account might otherwise merge past a rule as an admin."""
        first = state.blocked_rule != pr.head_sha
        state.blocked_on = "forge_rules"
        state.blocked_rule = pr.head_sha
        events = await self._set(
            ctx, profile, info, pr, state, "blocked", why, wake=True
        )
        return PlaybookResult(
            actions=[self._tell(pr, arm, "merge.blocked", why)] if first else [],
            events=events,
            # GitHub's verdict can trail the checks it waits on by a moment.
            wait=Wait(
                kinds=WAIT_KINDS,
                seconds=120 if first else RULES_SECONDS,
                reason="merge blocked",
            ),
            result={"phase": "blocked", "why": why},
        )

    async def _in_queue(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        info: RepoInfo,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        wait: Wait,
    ) -> PlaybookResult:
        """A PR in GitHub's merge queue merges there; one the queue let go (its CI
        on the queue failed) is blocked, to be queued again on a retry."""
        queued = await ctx.forge.in_merge_queue(pr.repo, pr.number)
        if queued is False:
            state.queued = False
            return await self._block(
                ctx,
                profile,
                pr,
                arm,
                state,
                "GitHub's merge queue let it go without merging it (its checks on "
                "the queue failed, or someone removed it); retry it to queue it again",
                False,
            )
        await self._set(
            ctx, profile, info, pr, state, "merging", "in GitHub's merge queue"
        )
        return PlaybookResult(
            wait=wait.model_copy(update={"reason": "in GitHub's merge queue"}),
            result={"phase": "merging"},
        )

    async def _never_reported(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        info: RepoInfo,
        state: ScheduleState,
        pending: list,
    ) -> str:
        """Why required checks the merge waits on will never report for ``pr``, when
        the base branch's workflows skip it; empty otherwise. The workflows are read
        once per head."""
        names = [
            i.name.split(":", 1)[1]
            for i in pending
            if i.name.startswith("check:") and i.detail == "not reported yet"
        ]
        mark = f"{pr.head_sha[:12]}:paths"
        if not names or info_of(pr.repo).kind != "github":
            return ""
        if mark in state.looked_into:
            # Read once per head; what it found stays the finding.
            return state.finding if NEVER_RUNS in state.finding else ""
        state.looked_into = [*state.looked_into[-9:], mark]
        folder = ".github/workflows"
        files: dict[str, str] = {}
        for name in (await ctx.forge.list_dir(pr.repo, folder, ref=pr.base_ref))[:60]:
            if name.endswith((".yml", ".yaml")):
                text = await ctx.forge.get_file(
                    pr.repo, f"{folder}/{name}", ref=pr.base_ref
                )
                if text:
                    files[name] = text
        skipped = never_run(files, names, pr.paths, pr.base_ref)
        if not skipped:
            return ""
        return (
            "; ".join(f"{check} {NEVER_RUNS} ({why})" for check, why in skipped.items())
            + ". The branch rules require it, so GitHub will not merge the PR until "
            "a maintainer changes the rule or the workflow, or merges it on GitHub"
        )

    async def _later(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        state: ScheduleState,
        kind: ActionKind,
        why: str,
    ) -> PlaybookResult:
        """Take a step the forge's rate limit refused again once the limit resets,
        as a new action, keeping the turn."""
        now = ctx.clock.now()
        found = re.search(r"rate limited until (\d+)", why)
        until = (
            datetime.fromtimestamp(int(found.group(1)), tz=now.tzinfo)
            if found
            else None
        )
        if until is None or until <= now:
            until = now + timedelta(seconds=RATE_LIMIT_SECONDS)
        state.not_before = until + timedelta(seconds=5)
        state.transient += 1
        if kind == ActionKind.PR_UPDATE_BRANCH:
            state.updates = max(0, state.updates - 1)
            state.updated_from = None
        if kind == ActionKind.PR_MERGE:
            state.queued = False
        await self._note(
            ctx,
            pr,
            state,
            state.phase if state.phase in ACTIVE else "queued",
            f"{why}; it tries again at {state.not_before:%H:%M} UTC",
        )
        return PlaybookResult(
            wait=Wait(
                kinds=WAIT_KINDS,
                seconds=max(30, int((state.not_before - now).total_seconds())),
                reason="the forge's rate limit",
            ),
            result={"phase": state.phase, "rate_limited": True},
        )

    async def _our_failure(
        self, ctx: PlaybookContext, event: Event, arm: MergeArm
    ) -> tuple[ActionKind, str, str] | None:
        """The kind of a step the schedule took for this PR that failed, why, and
        whose sign-in lacked a scope the forge asked for (``service``, ``person``,
        or empty); ``None`` when the failed action was not one of the schedule's."""
        record_ = await ctx.store.get_action(str(event.payload.get("action_id") or ""))
        if record_ is None or record_.action.provenance.scheduled_by != arm.armed_by:
            return None
        error = str(event.payload.get("error") or record_.error or "it failed")
        kind = record_.action.kind
        if "RateLimited" in error or "rate limit" in error.lower():
            return kind, f"{RATE_LIMITED} refused it: {error[:300]}", ""
        if "SAML" in error:
            return (
                kind,
                "GitHub refused it: the organization enforces SAML single sign-on, "
                "and the sign-in that wrote it is not authorized for it. "
                f"{error[:400]}",
                "",
            )
        if "workflow" in error and ("scope" in error or "OAuth App" in error):
            # GitHub lets no sign-in without the workflow scope write a change to a
            # workflow file: a merge or base merge of a PR that has one.
            act = "merge" if kind == ActionKind.PR_MERGE else "merge the base into"
            if record_.post_as:
                # A person's own sign-in: only they can sign in again, with it.
                return (
                    kind,
                    f"GitHub refused to {act} it as {record_.post_as}: a workflow "
                    f"file changes, and {record_.post_as}'s dashboard sign-in lacks "
                    "the workflow scope. They sign in to the dashboard again (it "
                    "asks for the scope now) and retry it",
                    "person",
                )
            return (
                kind,
                f"GitHub refused to {act} it: a workflow file changes, and the "
                "service's GitHub sign-in lacks the workflow scope. The service "
                "asks its owner to add it; once approved, it merges",
                "service",
            )
        if kind == ActionKind.PR_UPDATE_BRANCH:
            return kind, f"merging the base in failed: {error}", ""
        if kind == ActionKind.PR_FIX:
            what = "fix" if record_.action.payload.get("schedule_fix") else "resolution"
            return kind, f"pushing the schedule's {what} failed: {error}", ""
        if kind == ActionKind.PR_MERGE:
            return kind, f"the forge refused the merge: {error}", ""
        return kind, f"{kind} failed: {error}", ""

    async def _new_head(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        pr: PrSnapshot,
        arm: MergeArm,
        state: ScheduleState,
        event: Event,
    ) -> MergeArm | PlaybookResult:
        """The arm at the PR's new head when the schedule keeps it, else the result
        of unscheduling it. New commits keep it: whether approvals survive them is
        GitHub's to say (``RepoInfo.dismisses_stale_reviews``,
        ``requires_last_push_approval``), and the merge gate reads that, with CI at
        the new head. Only a follow-up review at the new head that finds a critical
        or major problem unschedules it. A base merge the schedule asked for, and a
        push its scheduler approved as themselves on a PR they scheduled by hand
        (``_scheduler_pushed``), keep the arm as seen; other commits mark it unseen
        (``MergeArm.unseen``)."""
        if (
            state.updated_from
            and same_commit(state.updated_from, arm.sha)
            and only_base_merged(pr, arm.sha, {arm.armed_by, *ctx.identities()})
        ):
            arm = arm.model_copy(update={"sha": pr.head_sha})
            await ctx.store.save_arm(arm)
            state.updated_from = None
            return arm
        pushed = await self._scheduler_pushed(ctx, pr, arm)
        if pushed == "pushing" and event.kind != EventKind.JOB_TIMEOUT:
            return PlaybookResult(
                wait=Wait(
                    kinds=WAIT_KINDS,
                    seconds=LANDING_SECONDS,
                    reason="waiting for the scheduler's push to be recorded",
                )
            )
        records = await ctx.store.list_review_records(pr.repo, pr.number)
        spot = next(
            (r for r in reversed(records) if same_commit(r.sha, pr.head_sha)), None
        )
        serious = [
            f
            for f in (spot.result.get("new_findings", []) if spot else []) or []
            if f.get("severity") in ("critical", "major")
        ]
        if not serious:
            arm = arm.model_copy(
                update={
                    "sha": pr.head_sha,
                    "unseen": arm.unseen or pushed != "pushed",
                }
            )
            await ctx.store.save_arm(arm)
            if state.phase == "failed":
                # Back from the failure lane, after every PR scheduled meanwhile.
                arm = await self._to_bottom(ctx, arm)
                state.phase = "queued"
            state.updates = 0
            state.cancelled = []
            state.cancel_to_rerun = []
            return arm
        await ctx.store.delete_arm(pr.repo, pr.number)
        left = await self._leave(ctx, profile, pr.repo, pr.number)
        return PlaybookResult(
            actions=[
                self._tell(
                    pr,
                    arm,
                    "merge.unscheduled",
                    f"the follow-up review of {short_sha(pr.head_sha)}, pushed after "
                    f"it was scheduled at {short_sha(arm.sha)}, found {len(serious)} "
                    "critical or major problem(s), so it is unscheduled; schedule it "
                    "again when they are addressed",
                )
            ],
            events=left.events,
            result={"done": "unscheduled after a follow-up review"},
        )

    async def _scheduler_pushed(
        self, ctx: PlaybookContext, pr: PrSnapshot, arm: MergeArm
    ) -> Literal["pushed", "pushing"] | None:
        """``pushed`` when the PR's head is a ``pr.fix`` onto its branch, from the
        armed head, that the maintainer who scheduled it by hand approved as
        themselves: they saw the change, and keeping their arm keeps the steps only
        their schedule approves (a fork's held workflows). ``pushing`` while such a
        push runs and its new head is not recorded yet. An arm automatic scheduling
        made is never kept this way: nobody saw the code it armed."""
        if arm.auto:
            return None
        records = await ctx.store.list_actions(
            repo=pr.repo,
            number=pr.number,
            kinds=[ActionKind.PR_FIX],
            statuses=[ActionStatus.EXECUTED, ActionStatus.INTENT],
        )
        theirs = [
            r
            for r in records
            if (r.post_as or "").lower() == arm.armed_by.lower()
            and r.action.payload.get("target") == "branch"
            and same_commit(r.action.payload.get("base_sha"), arm.sha)
        ]
        if any(
            r.status == ActionStatus.EXECUTED
            and same_commit(r.external_ref.get("sha"), pr.head_sha)
            for r in theirs
        ):
            return "pushed"
        if any(r.status == ActionStatus.INTENT for r in theirs):
            return "pushing"
        return None


def cancel_runs_action(
    pr: PrSnapshot,
    runs: dict[int, str],
    why: str,
    *,
    by: str,
    scheduled: int | None = None,
    tag: str = "",
) -> Action:
    """A ``ci.cancel`` of ``pr``'s unfinished workflow runs ``runs`` (run id to
    workflow), drafted for ``by``: the merge schedule's step for the PR ``by``
    scheduled at ``scheduled`` (its arming time), which the schedule approves, or a
    maintainer's press (``scheduled`` unset)."""
    action = make_action(
        ActionKind.CI_CANCEL,
        pr.repo,
        CancelRunsPayload(
            run_ids=sorted(runs),
            workflows=sorted(set(runs.values())),
            reason=why[:500],
        ),
        number=pr.number,
        head_sha=pr.head_sha,
        discriminator=(f"schedule:{scheduled}:" if scheduled is not None else "")
        + "cancel:"
        + ",".join(str(r) for r in sorted(runs))
        + (f":{tag}" if tag else ""),
        rationale=why,
        draft_for=by,
    )
    action.provenance.requested_by = by
    if scheduled is not None:
        action.provenance.scheduled_by = by
    return action


def _numbers(events: list[Event]) -> list[int]:
    return [e.number for e in events if e.number is not None]


async def maintains(
    ctx: PlaybookContext,
    profile: RepoProfile,
    repo: str,
    login: str,
    permission: Permission,
) -> bool:
    """Whether ``login`` may schedule merges, or have them scheduled as them: they
    hold ``Grant.MERGE_SCHEDULE`` in ``repo`` (design 2.7) and their forge
    permission satisfies ``merge.arming.who``. Scheduling approves the steps the
    schedule takes, a merge among them."""
    arming = profile.merge.arming.who
    if not permission.at_least(
        Permission.WRITE if arming == "codeowners" else Permission(arming)
    ):
        return False
    if ctx.access_for is not None:
        effective = await ctx.access_for(login, repo, permission)
        return effective.can(Grant.MERGE_SCHEDULE)
    named = await ctx.governance.maintainers_of(repo) if ctx.governance else set()
    if named:
        return login.lower() in {m.lower() for m in named}
    return permission.at_least(profile.access.configure)


async def arm_allowed(
    ctx: PlaybookContext, login: str, paths: list[str]
) -> tuple[bool, Permission, bool]:
    """Whether ``login`` may schedule a merge (``maintains``; with
    ``merge.arming.who: codeowners``, only a code owner of ``paths`` among them)."""
    profile = ctx.require_profile()
    permission = await ctx.permission(login)
    owner = login in await ctx.codeowners_for(paths)
    allowed = await maintains(ctx, profile, profile.repo, login, permission) and (
        owner or profile.merge.arming.who != "codeowners"
    )
    return allowed, permission, owner


async def gate_for(
    ctx: PlaybookContext,
    profile: RepoProfile,
    info: RepoInfo,
    pr: PrSnapshot,
    arm: MergeArm | None,
) -> GateResult:
    """The merge gate for ``pr`` from fresh reads; ``arm`` is ``None`` for a PR
    nobody scheduled."""
    repo, number = pr.repo, pr.number
    reviewers = {r.author for r in pr.reviews}
    perms = {login: await ctx.permission(login) for login in reviewers}
    hygiene = await ctx.store.get_state(hygiene_key(repo, number)) or {}
    blocking = {
        r["id"]: r["passed"]
        for r in hygiene.get("results", [])
        if r.get("blocking")
        and not r.get("skipped")
        and same_commit(hygiene.get("sha"), pr.head_sha)
    }
    missing_hygiene = [
        s.id
        for s in profile.checks
        if s.blocking and s.enabled and s.id not in blocking
    ]
    for check_id in missing_hygiene:
        blocking[check_id] = False
    paused = await paused_now(ctx, repo)
    return evaluate_merge_gate(
        pr,
        profile,
        info,
        arm,
        perms,
        ctx.identities(),
        blocking,
        paused,
        ctx.clock.now(),
    )


async def paused_now(ctx: PlaybookContext, repo: str) -> bool:
    if await ctx.store.get_state("paused"):
        return True
    record_ = await ctx.store.get_repo(repo)
    return bool(record_ and record_.paused)
