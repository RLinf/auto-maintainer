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
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    ApproveRunsPayload,
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
from ..domain.ids import same_commit, short_sha
from ..domain.merge_gate import (
    GateResult,
    MergeArm,
    evaluate_merge_gate,
    squash_message,
)
from ..domain.merge_schedule import (
    ACTIVE,
    LANDING_SECONDS,
    UPDATES_PER_TURN,
    WAITING,
    ScheduleState,
    holding_back,
    left_for_merge,
    only_base_merged,
    ordered,
    ready_conditions,
    serial,
    state_key,
    turns,
)
from ..domain.permissions import Permission
from ..domain.profile import RepoProfile
from ..domain.records import RootCause
from ..domain.review import ReviewState
from ..domain.run_ci import ci_view, latest_checks, rerun_targets
from ..domain.snapshot import PrSnapshot, PrState, RepoInfo
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
WAIT_SECONDS = 6 * 3600
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
            why = await self._our_failure(ctx, event, arm)
            if why:
                return await self._fail(ctx, profile, pr, arm, state, why)
        if not same_commit(arm.sha, pr.head_sha):
            moved = await self._new_head(ctx, profile, pr, arm, state, event)
            if isinstance(moved, PlaybookResult):
                return moved
            arm = moved
        info = await ctx.forge.get_repo(repo)
        gate = await gate_for(ctx, profile, info, pr, arm)
        wait = Wait(kinds=WAIT_KINDS, seconds=WAIT_SECONDS, reason="merge schedule")
        if state.phase == "failed" and same_commit(state.sha, pr.head_sha):
            ran = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start).state
            if ran not in ("running", "passed") or state.retry:
                return PlaybookResult(
                    wait=wait.model_copy(update={"reason": "in the failure lane"}),
                    result={"phase": "failed"},
                )
            # Someone ran its CI again (on the forge): it takes a turn again.
            state.phase = "queued"
            state.detail = "its CI ran again"
        conflict = next(
            (i for i in gate.items if i.name == "no_conflict" and not i.ok), None
        )
        if conflict is not None:
            return await self._fail(
                ctx,
                profile,
                pr,
                arm,
                state,
                f"it conflicts with {pr.base_ref}: merge {pr.base_ref} into it "
                "(Merge on the PR page), then retry",
            )
        ready = ready_conditions(profile, arm)
        back = holding_back(gate, ready)
        if back:
            first = back[0]
            detail = first.name.replace("_", " ") + (
                f": {first.detail}" if first.detail else ""
            )
            events = await self._set(
                ctx, profile, info, pr, state, "held", detail, wake=True
            )
            return PlaybookResult(
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
                    wait=wait.model_copy(
                        update={"reason": f"in the merge schedule; {detail}"}
                    ),
                    result={"phase": "queued", "turn": ahead[:1]},
                )
            if state.phase not in ACTIVE:
                # Take the turn before letting go of the lock.
                state.updates = 0
                await self._set(ctx, profile, info, pr, state, "starting", "its turn")
        return await self._act(ctx, profile, info, pr, arm, gate, state, wait, one)

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
    ) -> PlaybookResult:
        """The step a PR whose turn it is takes now."""
        now = ctx.clock.now()
        hints = ActionHints(
            author_is_team=await ctx.author_is_team(pr), changed_paths=pr.paths
        )

        def step(
            kind: ActionKind, payload: Any, discriminator: str, why: str
        ) -> Action:
            action = make_action(
                kind,
                pr.repo,
                payload,
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"schedule:{discriminator}",
                rationale=f"{why}; scheduled by {arm.armed_by}",
                draft_for=arm.armed_by,
                hints=hints,
            )
            action.provenance.scheduled_by = arm.armed_by
            action.provenance.requested_by = arm.armed_by
            return action

        if gate.outcome == "merge":
            subject, body = squash_message(pr, profile)
            methods = info.merge_methods
            method = (
                profile.merge.method
                if not methods or profile.merge.method in methods
                else methods[0]
            )
            action = step(
                ActionKind.PR_MERGE,
                MergePayload(
                    method=method, subject=subject, body=body, armed_by=arm.armed_by
                ),
                "merge",
                "the merge gate holds",
            )
            await self._set(ctx, profile, info, pr, state, "merging", "merging")
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
                f"update:{state.updates}",
                f"behind {pr.base_ref}, which it must be up to date with",
            )
            await self._set(
                ctx, profile, info, pr, state, "updating", f"merging {pr.base_ref} in"
            )
            return PlaybookResult(
                actions=[action], wait=wait, result={"phase": "updating"}
            )

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
            actions: list[Action] = []
            if arm.auto:
                state.approval_needed = True
                detail = (
                    "GitHub holds "
                    + ", ".join(workflows)
                    + " until a maintainer approves running them"
                )
            else:
                actions.append(
                    step(
                        ActionKind.CI_APPROVE_RUN,
                        ApproveRunsPayload(run_ids=runs, workflows=workflows),
                        "approve:" + ",".join(str(r) for r in runs),
                        "GitHub holds its workflows for a maintainer",
                    )
                )
                detail = "approving " + ", ".join(workflows)
            await self._set(ctx, profile, info, pr, state, "starting", detail)
            return PlaybookResult(
                actions=actions,
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
        if minutes >= limit:
            actions = await self._look_into(
                ctx, profile, pr, arm, state, what, minutes, step
            )
        detail = state.finding or (
            f"{view.passed} passed, {view.running} running"
            if phase == "running"
            else what
        )
        await self._set(ctx, profile, info, pr, state, phase, detail)
        left = max(60, int((limit - minutes) * 60)) if minutes < limit else WAIT_SECONDS
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
    ) -> PlaybookResult:
        """Rerun what flaked within the budget; otherwise the failure lane."""
        checks = failing_checks(pr, profile)
        if state.retry:
            # A maintainer sent it back from the failure lane: run what failed
            # again, past the budget, before judging it anew.
            state.retry = False
            view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
            runs = {c.run_id for c in [*checks, *view.cancelled] if c.run_id}
            targets = [
                t for t in rerun_targets(latest_checks(pr.checks)) if t.run_id in runs
            ]
            if targets:
                state.rerun_of = [c.id for c in [*checks, *view.cancelled]]
                await self._set(
                    ctx,
                    profile,
                    info,
                    pr,
                    state,
                    "rerunning",
                    "running what failed again, as a maintainer asked",
                )
                return PlaybookResult(
                    actions=[
                        step(
                            ActionKind.CI_RERUN_FAILED,
                            RerunPayload(
                                run_id=t.run_id, workflow=t.workflow, attempt=t.attempt
                            ),
                            f"retry:{t.run_id}:{state.retries}",
                            "a maintainer sent it back to run again",
                        )
                        for t in targets
                    ],
                    wait=wait,
                    result={"phase": "rerunning", "retry": state.retries},
                )
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
                    ctx, profile, pr, arm, state, "its CI was cancelled, again"
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
        reruns: list[Action] = []
        retrying: set[int] = set()
        rerun = profile.ci.rerun
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
            retrying.add(f.check.id)
            if f.check.run_id not in {a.payload["run_id"] for a in reruns}:
                reruns.append(
                    step(
                        ActionKind.CI_RERUN_FAILED,
                        RerunPayload(
                            run_id=f.check.run_id,
                            workflow=f.check.workflow,
                            attempt=f.check.run_attempt,
                        ),
                        f"rerun:{f.check.run_id}:{done}",
                        f"{f.check.name}: {f.diagnosis.explanation}",
                    )
                )
        stuck = [f for f in failures if f.check.id not in retrying]
        found: tuple[list[RootCause] | None, str] | None = None
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
                retrying.add(f.check.id)
                if f.check.run_id not in {a.payload["run_id"] for a in reruns}:
                    reruns.append(
                        step(
                            ActionKind.CI_RERUN_FAILED,
                            RerunPayload(
                                run_id=f.check.run_id,
                                workflow=f.check.workflow,
                                attempt=f.check.run_attempt,
                            ),
                            f"rerun:{f.check.run_id}:{done}",
                            f"{f.check.name}: {cause.cause}",
                        )
                    )
            stuck = [f for f in failures if f.check.id not in retrying]
        if stuck or not reruns:
            jobs = ", ".join(f.check.name for f in stuck or failures) or "CI"
            return await self._fail(
                ctx, profile, pr, arm, state, f"{jobs} failed", failures, found
            )
        await record(ctx, pr, failures, found[0] if found else None)
        state.rerun_of = [f.check.id for f in failures]
        await self._set(
            ctx,
            profile,
            info,
            pr,
            state,
            "rerunning",
            "rerunning " + ", ".join(f.check.name for f in failures),
        )
        return PlaybookResult(actions=reruns, wait=wait, result={"phase": "rerunning"})

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
        if found.step == "approve_runs" and not arm.auto:
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
        state.failed_jobs = [f.check.name for f in failures or [] if f.real]
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
        actions.append(self._tell(pr, arm, "merge.failed", why))
        info = await ctx.forge.get_repo(pr.repo)
        events = await self._set(
            ctx, profile, info, pr, state, "failed", why, wake=True
        )
        return PlaybookResult(
            actions=actions,
            events=events,
            wait=Wait(
                kinds=WAIT_KINDS, seconds=WAIT_SECONDS, reason="in the failure lane"
            ),
            result={"phase": "failed", "why": why},
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
        if wake and (was_active or phase in ("failed", "held", "approval")):
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

    async def _our_failure(
        self, ctx: PlaybookContext, event: Event, arm: MergeArm
    ) -> str | None:
        """Why a step the schedule took for this PR failed, or ``None`` when the
        failed action was not one of them."""
        record_ = await ctx.store.get_action(str(event.payload.get("action_id") or ""))
        if record_ is None or record_.action.provenance.scheduled_by != arm.armed_by:
            return None
        error = str(event.payload.get("error") or record_.error or "it failed")
        kind = record_.action.kind
        if kind == ActionKind.PR_UPDATE_BRANCH:
            return f"merging the base in failed: {error}"
        if kind == ActionKind.PR_MERGE:
            return f"the forge refused the merge: {error}"
        return f"{kind} failed: {error}"

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
        of unscheduling it. A base merge the schedule asked for keeps it; other
        commits keep it only when the approval survives and the follow-up spot-check
        finds no regression."""
        if (
            state.updated_from
            and same_commit(state.updated_from, arm.sha)
            and only_base_merged(pr, arm.sha, {arm.armed_by, *ctx.identities()})
        ):
            arm = arm.model_copy(update={"sha": pr.head_sha})
            await ctx.store.save_arm(arm)
            state.updated_from = None
            return arm
        cls = ctx.classify(pr)
        records = await ctx.store.list_review_records(pr.repo, pr.number)
        spot = next(
            (r for r in reversed(records) if same_commit(r.sha, pr.head_sha)), None
        )
        approved = cls.state != ReviewState.FOLLOWUP_REVIEW and any(
            r.state.value == "APPROVED" for r in pr.reviews
        )
        clean = spot is not None and not any(
            f.get("severity") in ("critical", "major")
            for f in spot.result.get("new_findings", []) or []
        )
        if approved and clean:
            arm = arm.model_copy(update={"sha": pr.head_sha})
            await ctx.store.save_arm(arm)
            if state.phase == "failed":
                state.phase = "queued"
            state.updates = 0
            return arm
        if approved and spot is None and event.kind != EventKind.JOB_TIMEOUT:
            return PlaybookResult(
                wait=Wait(
                    kinds=WAIT_KINDS,
                    seconds=2 * 3600,
                    reason="waiting for the follow-up spot-check",
                )
            )
        # The maintainer scheduled the code they saw; new code waits for them.
        await ctx.store.delete_arm(pr.repo, pr.number)
        left = await self._leave(ctx, profile, pr.repo, pr.number)
        return PlaybookResult(
            actions=[
                self._tell(
                    pr,
                    arm,
                    "merge.unscheduled",
                    f"new commits arrived after it was scheduled at "
                    f"{short_sha(arm.sha)}, so it is unscheduled; schedule it again "
                    f"at {short_sha(pr.head_sha)} when it is ready",
                )
            ],
            events=left.events,
            result={"done": "unscheduled after a push"},
        )


async def maintains(
    ctx: PlaybookContext,
    profile: RepoProfile,
    repo: str,
    login: str,
    permission: Permission,
) -> bool:
    """Whether ``login`` may schedule merges, or have them scheduled as them: they
    maintain ``repo`` (its People record's maintainers, else ``access.configure``,
    as the configuration gate reads it) and may arm merges (``merge.arming.who``).
    Scheduling approves the steps the schedule takes, a merge among them."""
    arming = profile.merge.arming.who
    if not permission.at_least(
        Permission.WRITE if arming == "codeowners" else Permission(arming)
    ):
        return False
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
