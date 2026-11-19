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


"""The Merge scheduling page's API (design 5.5): what is scheduled and how it goes,
the failure lane, the candidates, and the PRs not ready yet, with the rules they
are read by; and a maintainer's scheduling, ordering, retrying, and settings.

Anyone who may view the repository sees the page. Scheduling, ordering, retrying,
unscheduling, and asking an agent to investigate need a maintainer
(``api.is_maintainer``, the configuration gate's test); the settings (merge
scheduling on, automatic scheduling, how many at once, how CI starts) are profile
changes and go through the gate as any other.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.memory import profile_path
from ..domain.merge_gate import GateResult, MergeArm, evaluate_merge_gate
from ..domain.merge_schedule import (
    ACTIVE,
    ScheduleState,
    fix_key,
    holding_back,
    next_position,
    ordered,
    position,
    ready_conditions,
    resolve_key,
    serial,
    skip_key,
    state_key,
)
from ..domain.permissions import Permission
from ..domain.profile import (
    FlakeSignature,
    ReadyCondition,
    RepoProfile,
    ResolveKind,
    ScheduleFix,
)
from ..domain.records import CiObservation, JobStatus
from ..domain.run_ci import ci_view, latest_checks, unfinished_runs
from ..domain.snapshot import CheckStatus, Mergeable, PrSnapshot, PrState, RepoInfo
from ..playbooks.auto_schedule import AUTO_COMMAND
from ..playbooks.ci_rules import COMMAND as CI_RULES
from ..playbooks.ci_rules import (
    DRAFT_KEY,
    MAX_PATTERNS,
    TRAILER,
    CiRulesDraft,
    FlakePattern,
    current,
)
from ..playbooks.flake_learn import COMMAND as LEARN_COMMAND
from ..playbooks.flake_learn import SUGGESTIONS
from ..playbooks.hygiene import hygiene_key
from ..playbooks.merge import MERGED_KEY, cancel_runs_action
from ..playbooks.prompts import untrusted
from ..profiles.layers import ProfileError, parse_yaml
from ..service import Service
from .api import (
    _repo,
    is_maintainer,
    permissions_of,
    require_propose,
    require_view,
    service_of,
    shows_maintainer,
)
from .api import (
    _run_now as run_now,
)
from .auth import User, current_user


class ScheduleBody(BaseModel):
    number: int = Field(ge=1)
    force: bool = False
    """Schedule it by hand though it does not meet the repository's target yet."""


class OrderBody(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=500)


class SettingsBody(BaseModel):
    enabled: bool | None = None
    auto: bool | None = None
    parallel: int | None = Field(default=None, ge=1, le=20)


class FreezeBody(BaseModel):
    start: datetime
    end: datetime
    reason: str = Field(default="", max_length=300)


class ResolveBody(BaseModel):
    enabled: bool
    union_only: bool = True
    kinds: list[ResolveKind] = Field(default_factory=list, max_length=20)


class MergingBody(BaseModel):
    """How the repository's scheduled PRs merge; each field left out stays as it is."""

    enabled: bool | None = None
    method: Literal["squash", "merge", "rebase"] | None = None
    subject: str | None = Field(default=None, max_length=200)
    preserve_trailers: list[str] | None = Field(default=None, max_length=20)
    signoff_from_authors: bool | None = None
    update_branch: bool | None = None
    parallel: int | None = Field(default=None, ge=1, le=20)
    required_workflows: list[str] | None = Field(default=None, max_length=30)
    cancelled_is_failure: bool | None = None
    approvals: int | None = Field(default=None, ge=0, le=20)
    arming_who: Literal["write", "maintain", "admin", "codeowners"] | None = None
    freezes: list[FreezeBody] | None = Field(default=None, max_length=50)
    resolve: ResolveBody | None = None
    fix: ScheduleFix | None = None


class CiStartBody(BaseModel):
    how: str = Field(default="", max_length=8_000)
    gate_label: str | None = Field(default=None, max_length=200)
    gate_jobs: list[str] = Field(default_factory=list, max_length=50)
    summary_jobs: list[str] = Field(default_factory=list, max_length=50)
    start_minutes: int = Field(default=5, ge=1, le=24 * 60)
    run_minutes: int = Field(default=180, ge=5, le=7 * 24 * 60)
    optional_checks: list[str] = Field(default_factory=list, max_length=50)
    ready: list[ReadyCondition] | None = None
    """What a PR must meet to be scheduled; ``None`` leaves it as it is."""
    merge_notes: str | None = Field(default=None, max_length=8_000)
    approvals: int | None = Field(default=None, ge=0, le=20)


class DescribeBody(BaseModel):
    text: str = Field(min_length=3, max_length=8_000)
    """The rules in the maintainer's own words."""


class FlakyBody(BaseModel):
    diagnose: Literal["agent", "patterns"]
    max_reruns: int = Field(ge=0, le=10)
    backoff_seconds: int = Field(ge=0, le=24 * 3600)
    rerun_unknown: int = Field(ge=0, le=5)
    root_cause: bool


class PatternsBody(BaseModel):
    patterns: list[FlakePattern] = Field(max_length=MAX_PATTERNS)
    """The repository's own flake patterns; a pack's stay as they are."""


class VerdictBody(BaseModel):
    verdict: Literal["flaky", "real"] | None


class BlockingBody(BaseModel):
    roles: list[str] = Field(max_length=50)
    """The label roles whose label holds a PR's merge back."""


class ReadyBody(BaseModel):
    ready: list[ReadyCondition] | None
    """``None`` for every condition the merge rules hold a PR to."""


async def gate_of(
    service: Service,
    profile: RepoProfile,
    info: RepoInfo,
    pr: PrSnapshot,
    arm: MergeArm | None,
    known: dict[str, Permission] | None = None,
) -> GateResult:
    """The merge gate for ``pr`` as the merge job reads it, from what the service
    has: reviewers' permissions cached for the views (``known``, by lowercase login,
    when the caller read them for many PRs at once; the rest read in parallel), the
    hygiene results kept."""
    known = dict(known or {})
    missing = [r.author for r in pr.reviews if r.author.lower() not in known]
    if missing:
        known.update(await permissions_of(service, pr.repo, missing))
    perms = {r.author: known[r.author.lower()] for r in pr.reviews}
    hygiene = await service.store.get_state(hygiene_key(pr.repo, pr.number)) or {}
    blocking = {
        r["id"]: r["passed"]
        for r in hygiene.get("results", [])
        if r.get("blocking")
        and not r.get("skipped")
        and same_commit(hygiene.get("sha"), pr.head_sha)
    }
    for s in profile.checks:
        if s.blocking and s.enabled and s.id not in blocking:
            blocking[s.id] = False
    return evaluate_merge_gate(
        pr,
        profile,
        info,
        arm,
        perms,
        service.info.identities,
        blocking,
        await service.pipeline.paused(pr.repo),
        service.clock.now(),
    )


async def schedule_brief(service: Service, repo: str, number: int) -> list[str]:
    """What the agent is told about ``number``'s place in the merge schedule when a
    person asks about it from the Merge page (``web/chat.py:_page_block``): who
    scheduled it and at which head, where it stands in the order, its phase, the
    merge rules as the service reads them, GitHub's own verdict, and the steps the
    schedule took since it was scheduled. Root causes and findings came from agents
    reading CI logs, so they go in as untrusted data."""
    arm = await service.store.get_arm(repo, number)
    if arm is None:
        return [f"{repo}#{number} is not in the merge schedule now."]
    state = await _state(service, repo, number)
    arms = ordered(await service.store.list_arms(repo))
    flags = [
        "by automatic scheduling" if arm.auto else "",
        "by hand before it met the conditions to schedule" if arm.forced else "",
        "with commits pushed since that its scheduler has not seen (a fork's held "
        "workflows wait for them to approve running them)"
        if arm.unseen
        else "",
    ]
    lines = [
        f"The person asks about {repo}#{number} in the merge schedule. It was "
        f"scheduled by {arm.armed_by} at {arm.armed_at:%Y-%m-%d %H:%M} UTC, now at "
        f"head {arm.sha[:12]}" + "".join(f", {f}" for f in flags if f) + ".",
        "The order (lowest first; one PR takes the turn at a time when PRs must be "
        "up to date with the base): " + ", ".join(f"#{a.number}" for a in arms) + ".",
        f"Its phase: {state.phase}"
        + (f" since {state.since:%Y-%m-%d %H:%M} UTC" if state.since else "")
        + (f"; {state.detail}" if state.detail else "")
        + (f" (blocked on {state.blocked_on})" if state.phase == "blocked" else "")
        + f". Merge attempts at this head: {state.merge_attempts}.",
    ]
    profile = await service.profiles.active(repo)
    pr = await service.store.get_snapshot(repo, number) or await service.a.forge.get_pr(
        repo, number
    )
    if profile is not None and pr is not None:
        info = await service.view_repo(repo)
        gate = await gate_of(service, profile, info, pr, arm)
        lines.append(
            "The merge rules as the service reads them: "
            + "; ".join(
                f"{i.name} "
                + ("ok" if i.ok else "pending" if i.pending else "not met")
                + (f" ({i.detail})" if i.detail and not i.ok else "")
                for i in gate.items
            )
            + f". GitHub's own verdict: {pr.merge_state or 'unknown'}, mergeable "
            f"{pr.mergeable.value.lower()}."
        )
    found = [*state.causes, state.finding] if state.finding else list(state.causes)
    if found:
        lines.append(untrusted("merge.root_causes", "\n".join(found), 4000))
    steps = [
        r
        for r in await service.store.list_actions(
            repo=repo, number=number, since=arm.armed_at, limit=200
        )
        if r.action.provenance.scheduled_by
        or (
            r.action.kind == ActionKind.NOTIFY
            and str(r.action.payload.get("event") or "").startswith("merge.")
        )
    ]
    if steps:
        lines.append(
            untrusted(
                "merge.steps",
                "\n".join(
                    f"{r.action.created_at:%m-%d %H:%M} "
                    + (
                        f"said: {r.action.payload.get('text')}"
                        if r.action.kind == ActionKind.NOTIFY
                        else f"{r.action.kind} {r.status.value}"
                        + (f" as {r.post_as}" if r.post_as else "")
                        + (f": {r.action.rationale}" if r.action.rationale else "")
                        + (f" (error: {r.error[:300]})" if r.error else "")
                    )
                    for r in sorted(steps, key=lambda r: r.action.created_at)[-60:]
                ),
                12000,
            )
        )
    lines.append(
        "Explain from these why it is where it is and what would move it on; the "
        "person may retry, reorder, or unschedule it on the Merge page."
    )
    return lines


async def _ci_now(
    service: Service, repo: str, arms: list[MergeArm], prs: Any
) -> list[dict[str, Any]]:
    """The open PRs whose workflow runs hold or wait for CI machines now, from the
    snapshots the service keeps: the scheduled PR holding the turn first, then the
    rest by number."""
    armed = {a.number: a for a in arms}
    rows = []
    for pr in prs:
        if pr.state != PrState.OPEN:
            continue
        runs = unfinished_runs(pr.checks)
        if not runs:
            continue
        arm = armed.get(pr.number)
        phase = (await _state(service, repo, pr.number)).phase if arm else None
        started = [
            c.started_at
            for c in latest_checks(pr.checks)
            if c.run_id in runs and c.started_at is not None
        ]
        rows.append(
            {
                "number": pr.number,
                "title": pr.title,
                "author": pr.author,
                "url": pr.url,
                "workflows": sorted(set(runs.values())),
                "runs": len(runs),
                "jobs": sum(
                    1
                    for c in latest_checks(pr.checks)
                    if c.run_id in runs and c.status != CheckStatus.COMPLETED
                ),
                "since": min(started).isoformat() if started else None,
                "scheduled": arm is not None,
                "holds_turn": phase in ACTIVE,
            }
        )
    rows.sort(key=lambda r: (not r["holds_turn"], r["number"]))
    return rows


async def _cancel_ci(
    service: Service, user: User, pr: PrSnapshot, why: str
) -> dict[str, Any] | None:
    """Cancel ``pr``'s unfinished workflow runs as ``user`` pressed it; a scheduled
    PR runs them again on its own turn (``ScheduleState.cancelled``). ``None`` when
    none runs."""
    runs = unfinished_runs(pr.checks)
    if not runs:
        return None
    if await service.store.get_arm(pr.repo, pr.number) is not None:
        state = await _state(service, pr.repo, pr.number)
        state.cancelled = sorted({*state.cancelled, *runs})
        await service.store.set_state(
            state_key(pr.repo, pr.number), state.model_dump(mode="json")
        )
    record = await run_now(
        service, user, cancel_runs_action(pr, runs, why, by=user.login)
    )
    return {"status": record.status, "runs": sorted(runs), "error": record.error}


def _item(name: str, detail: str) -> str:
    return name.replace("_", " ") + (f": {detail}" if detail else "")


def _row(pr: PrSnapshot, profile: RepoProfile) -> dict[str, Any]:
    view = ci_view(pr, profile.label_for("ci_gate"), profile.ci.start)
    return {
        "number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "url": pr.url,
        "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
        "behind": bool(pr.behind_base),
        # Conflicting with its base: the page offers to resolve it (an agent merges
        # the base in, and the person who asked pushes it).
        "conflict": pr.mergeable == Mergeable.CONFLICTING,
        "base": pr.base_ref,
        "ci": {
            "state": view.state,
            "passed": view.passed,
            "running": view.running,
            "failed": [c.name for c in view.failed],
        },
    }


async def _state(service: Service, repo: str, number: int) -> ScheduleState:
    raw = await service.store.get_state(state_key(repo, number))
    return (
        ScheduleState.model_validate(raw) if isinstance(raw, dict) else ScheduleState()
    )


async def _save_layer(
    service: Service,
    user: User,
    repo: str,
    change: Callable[[dict[str, Any]], None],
    summary: str,
) -> dict[str, Any]:
    """Change the repository's settings layer as ``user``: applied when they
    maintain it (the configuration gate), proposed to its maintainers otherwise."""
    current = await service.profiles.active(repo)
    if current is None:
        raise HTTPException(409, f"{repo} has no approved profile yet")
    layer = parse_yaml(
        await service.a.memory.read_text(profile_path(repo)) or "", "settings"
    )
    change(layer)
    await require_propose(service, user, repo)
    try:
        merged, memory_change = await service.profiles.propose_memory_layer(
            repo, layer, user.login, summary
        )
    except ProfileError as e:
        raise HTTPException(422, str(e)) from e
    decision = await service.gate.check(
        repo, user.login, current, merged.profile, person=True
    )
    if decision.allowed:
        memory_change.approvals = {repo: [user.login]}
    memory_change.requested_by = user.login
    await service.store.save_memory_change(memory_change)
    await service.submit_memory_change(memory_change)
    stored = await service.store.get_memory_change(memory_change.id)
    return {
        "applied": bool(stored and stored.state == "merged"),
        "change": stored.model_dump(mode="json") if stored else None,
        "reason": decision.reason,
    }


def _layer_patterns(layer: dict[str, Any]) -> list[dict[str, Any]]:
    ci = layer.get("ci") if isinstance(layer.get("ci"), dict) else {}
    found = ci.get("flake_signatures") if isinstance(ci, dict) else None
    return [p for p in found or [] if isinstance(p, dict)]


def draft_of(stored: dict[str, Any], profile: RepoProfile) -> CiRulesDraft:
    """The agent's stored draft, every setting it does not name (one drafted before
    the draft named it) taken from the repository's current settings, so saving it
    changes only what the agent drafted."""
    now = current(profile).model_dump(mode="json")
    drafted = stored.get("draft") if isinstance(stored.get("draft"), dict) else {}
    now.update({k: v for k, v in drafted.items() if k in now})
    return CiRulesDraft.model_validate(now)


async def _own_pattern_ids(service: Service, repo: str) -> set[str]:
    """The flake patterns the repository's settings layer names itself; the rest
    come from its packs."""
    layer = parse_yaml(
        await service.a.memory.read_text(profile_path(repo)) or "", "settings"
    )
    return {str(p.get("id")) for p in _layer_patterns(layer)}


def _apply_draft(
    layer: dict[str, Any], draft: CiRulesDraft, inherited: dict[str, FlakeSignature]
) -> None:
    """Write every setting of ``draft`` into ``layer``."""
    start = _section(layer, "ci", "start")
    start.update(
        {
            "how": draft.how.strip(),
            "gate_jobs": draft.gate_jobs,
            "summary_jobs": draft.summary_jobs,
            "start_minutes": draft.start_minutes,
            "run_minutes": draft.run_minutes,
            "optional_checks": draft.optional_checks,
        }
    )
    roles = _section(layer, "labels", "roles")
    if draft.gate_label:
        roles["ci_gate"] = draft.gate_label
    else:
        roles.pop("ci_gate", None)
    merge = _section(layer, "merge")
    merge["blocking_roles"] = draft.blocking_roles
    plan = _section(merge, "schedule")
    plan["ready"] = draft.ready
    if draft.merge_notes.strip():
        plan["notes"] = draft.merge_notes.strip()
    if draft.approvals:
        merge["approvals"] = max(int(merge.get("approvals") or 0), draft.approvals)
    merge["method"] = draft.merge_method
    merge["update_branch"] = draft.update_branch
    message = _section(merge, "message")
    message["subject"] = draft.subject
    message["preserve_trailers"] = draft.preserve_trailers
    message["signoff_from_authors"] = draft.signoff_from_authors
    _section(merge, "arming")["who"] = draft.arming_who
    resolve = plan.get("resolve") if isinstance(plan.get("resolve"), dict) else {}
    plan["resolve"] = {
        **resolve,
        "enabled": draft.resolve_conflicts,
        "kinds": [k.model_dump() for k in draft.resolve_kinds],
    }
    plan["parallel"] = draft.parallel
    ci = _section(layer, "ci")
    ci["diagnose"] = draft.diagnose
    ci["root_cause"] = draft.root_cause
    ci["required_workflows"] = draft.required_workflows
    ci["cancelled_is_failure"] = draft.cancelled_is_failure
    ci["rerun"] = {
        **(ci.get("rerun") if isinstance(ci.get("rerun"), dict) else {}),
        "max_per_sha": draft.max_reruns,
        "backoff_seconds": draft.backoff_seconds,
        "unknown": draft.rerun_unknown,
    }
    own = []
    for p in draft.flake_patterns:
        was = inherited.get(p.id)
        if was is not None and (was.pattern, was.category, was.description) == (
            p.pattern,
            p.category,
            p.description,
        ):
            continue  # a pack's, unchanged
        own.append(p.model_dump(mode="json"))
    if own:
        ci["flake_signatures"] = own
    else:
        ci.pop("flake_signatures", None)


async def _judge(
    service: Service, repo: str, obs_id: str, verdict: str | None, by: str
) -> CiObservation:
    """Record ``by``'s verdict on observation ``obs_id`` of ``repo``; a flaky one
    has an agent learn a flake pattern from the job's log."""
    known = {o.id: o for o in await service.store.list_ci_observations(repo)}
    if obs_id not in known:
        raise HTTPException(404, f"{repo} has no CI observation {obs_id}")
    obs = await service.store.set_ci_verdict(
        obs_id,
        verdict,
        by if verdict else None,
        service.clock.now() if verdict else None,
    )
    assert obs is not None
    if verdict == "flaky":
        await service.bus.publish(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo=repo,
                number=obs.number,
                payload={"command": LEARN_COMMAND, "by": by, "observation": obs_id},
            )
        )
    return obs


def _section(layer: dict[str, Any], *path: str) -> dict[str, Any]:
    node = layer
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    return node


async def _scope_view(service: Service, user: User) -> dict[str, Any] | None:
    """The latest request to add a scope to the service's sign-in: its one-time code
    only for the service's owner, who approves it as the service's account."""
    found = service.scope_request()
    if found is None:
        return None
    owner = (await service.service_gate(user.login)).allowed
    shown = {k: found[k] for k in ("scopes", "login", "state", "started_at")}
    if owner and found["state"] == "pending":
        shown |= {"code": found["code"], "url": found["url"]}
    return {**shown, "owner": owner}


def _merging(profile: RepoProfile, info: RepoInfo) -> dict[str, Any]:
    """How the repository's scheduled PRs merge, for the Merging card, with what the
    forge's rules mean for the schedule."""
    merge = profile.merge
    warnings: list[str] = []
    base_merges = bool(info.requires_up_to_date) or merge.update_branch
    if base_merges and info.dismisses_stale_reviews:
        warnings.append(
            "GitHub dismisses approvals when new commits arrive here, so each time "
            "the schedule merges the base into a PR, its approvals are dropped and "
            "it waits to be approved again."
        )
    if base_merges and info.requires_last_push_approval:
        warnings.append(
            "GitHub requires the last push to be approved by someone other than who "
            "pushed it, so after the schedule merges the base in (as the maintainer "
            "who scheduled the PR), another person approves it again."
        )
    if info.merge_queue:
        warnings.append(
            "The branch merges only through GitHub's merge queue: the schedule adds "
            "PRs to the queue, which merges them with its own commit message."
        )
    resolve = merge.schedule.resolve
    return {
        "enabled": merge.enabled,
        "method": merge.method,
        "methods": info.merge_methods,
        "subject": merge.message.subject,
        "preserve_trailers": merge.message.preserve_trailers,
        "signoff_from_authors": merge.message.signoff_from_authors,
        "update_branch": merge.update_branch,
        "requires_up_to_date": info.requires_up_to_date,
        "parallel": merge.schedule.parallel,
        "required_workflows": profile.ci.required_workflows,
        "cancelled_is_failure": profile.ci.cancelled_is_failure,
        "approvals": merge.approvals,
        "approvals_required": info.required_approvals,
        "arming_who": merge.arming.who,
        "freezes": [f.model_dump(mode="json") for f in merge.freezes],
        "resolve": resolve.model_dump(mode="json"),
        "fix": merge.schedule.fix.model_dump(mode="json"),
        "merge_queue": info.merge_queue,
        "warnings": warnings,
    }


async def _maintainer(service: Service, user: User, repo: str) -> None:
    if not await is_maintainer(service, user, repo):
        raise HTTPException(403, f"only {repo}'s maintainers arrange its merges")


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/merge-schedule")

    @r.get("/{repo:path}/view")
    async def view(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Everything the page shows for ``repo``."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)

        async def compute() -> dict[str, Any]:
            # What every viewer sees, computed once for all the tabs that ask at
            # the same moment (``web/recent.py``); each viewer's own fields follow.
            profile = await service.profiles.active(repo)
            if profile is None:
                raise HTTPException(409, f"{repo} has no approved profile yet")
            info = await service.view_repo(repo)
            arms = ordered(await service.store.list_arms(repo))
            armed = {a.number: a for a in arms}
            jobs: dict[int, dict[str, Any]] = {}
            rules_job: dict[str, Any] | None = None
            for job in await service.store.list_jobs(
                [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING]
            ):
                if job.repo != repo:
                    continue
                if job.playbook == "ci_rules":
                    rules_job = {"id": job.id, "status": job.status}
                elif (
                    job.playbook in ("merge", "ci_doctor", "merge_base", "fix_ci")
                    and job.number is not None
                ):
                    # The live view of the work on the PR; a running one first.
                    was = jobs.get(job.number)
                    if was is None or job.status == JobStatus.RUNNING:
                        jobs[job.number] = {
                            "id": job.id,
                            "status": job.status,
                            "playbook": job.playbook,
                        }
            snaps = {p.number: p for p in await service.store.list_snapshots(repo)}
            scheduled, failed = [], []
            for arm in arms:
                pr = snaps.get(arm.number) or await service.store.get_snapshot(
                    repo, arm.number
                )
                if pr is None:
                    continue
                state = await _state(service, repo, arm.number)
                row = {
                    **_row(pr, profile),
                    "phase": state.phase,
                    "detail": state.detail,
                    "blocked_on": state.blocked_on if state.phase == "blocked" else "",
                    "since": state.since.isoformat() if state.since else None,
                    "scheduled_by": arm.armed_by,
                    "auto": arm.auto,
                    "failed_jobs": state.failed_jobs,
                    "causes": state.causes,
                    "finding": state.finding,
                    "approval_needed": state.approval_needed,
                    "stale": not same_commit(arm.sha, pr.head_sha),
                    "unseen": arm.unseen,
                    "job": jobs.get(arm.number),
                    "forced": arm.forced,
                }
                (failed if state.phase == "failed" else scheduled).append(row)
            candidates, not_ready = [], []
            unarmed = [
                pr
                for pr in sorted(snaps.values(), key=lambda p: p.number)
                if pr.number not in armed
                and pr.state == PrState.OPEN
                and (not pr.is_draft or profile.scope.include_drafts)
            ]
            # Every reviewer's permission in one parallel read: once their cache time
            # runs out, reading them PR by PR held the page for tens of seconds.
            known = await permissions_of(
                service, repo, [r.author for pr in unarmed for r in pr.reviews]
            )
            for pr in unarmed:
                gate = await gate_of(service, profile, info, pr, None, known)
                back = holding_back(gate, ready_conditions(profile))
                row = _row(pr, profile)
                if not back:
                    candidates.append(row)
                else:
                    row["reasons"] = [_item(i.name, i.detail) for i in back[:3]]
                    not_ready.append(row)
            draft = await service.store.get_state(DRAFT_KEY.format(repo=repo))
            own_patterns = await _own_pattern_ids(service, repo)
            start = profile.ci.start
            return {
                "repo": repo,
                "enabled": profile.merge.enabled,
                "mode": "serial" if serial(info, profile) else "parallel",
                "parallel": profile.merge.schedule.parallel,
                "auto": profile.merge.schedule.auto,
                "auto_by": profile.merge.schedule.auto_by,
                "paused": await service.pipeline.paused(repo),
                "checking": service.checking_prs(repo),
                "check_progress": service.check_progress(repo),
                "rules": {
                    "approvals": max(
                        profile.merge.approvals, info.required_approvals or 0
                    ),
                    "code_owners": info.requires_code_owner_reviews,
                    "last_push": info.requires_last_push_approval,
                    "threads": True,
                    "up_to_date": info.requires_up_to_date,
                    "update_branch": profile.merge.update_branch,
                    "required_checks": info.required_checks,
                    "required_workflows": profile.ci.required_workflows,
                    "methods": info.merge_methods,
                    "method": profile.merge.method,
                    "fork_approval": info.fork_approval,
                    "blocking_labels": [
                        label
                        for role in profile.merge.blocking_roles
                        if (label := profile.label_for(role))
                    ],
                    "notes": profile.merge.schedule.notes,
                    "subject": profile.merge.message.subject,
                    "preserve_trailers": profile.merge.message.preserve_trailers,
                    "cancelled_is_failure": profile.ci.cancelled_is_failure,
                    "hygiene": [
                        s.id for s in profile.checks if s.blocking and s.enabled
                    ],
                    "ready": list(ready_conditions(profile)),
                    "ready_set": profile.merge.schedule.ready is not None,
                    "blocking_roles": profile.merge.blocking_roles,
                    "label_roles": dict(sorted(profile.labels.roles.items())),
                },
                "merging": _merging(profile, info),
                "flaky": {
                    "diagnose": profile.ci.diagnose,
                    "max_reruns": profile.ci.rerun.max_per_sha,
                    "backoff_seconds": profile.ci.rerun.backoff_seconds,
                    "rerun_unknown": profile.ci.rerun.unknown,
                    "root_cause": profile.ci.root_cause,
                    "patterns": [
                        {
                            **sig.model_dump(
                                mode="json",
                                include={"id", "pattern", "category", "description"},
                            ),
                            "own": sig.id in own_patterns,
                        }
                        for sig in profile.ci.flake_signatures
                    ],
                    "suggestions": await service.store.get_state(
                        SUGGESTIONS.format(repo=repo)
                    )
                    or [],
                },
                "ci_start": {
                    **start.model_dump(mode="json"),
                    "gate_label": profile.label_for("ci_gate"),
                    "reruns": profile.ci.rerun.max_per_sha,
                },
                "draft": {
                    **draft,
                    "draft": draft_of(draft, profile).model_dump(mode="json"),
                }
                if isinstance(draft, dict)
                else None,
                "rules_job": rules_job,
                "scheduled": scheduled,
                "failed": failed,
                "ci_now": await _ci_now(service, repo, arms, snaps.values()),
                "candidates": candidates,
                "not_ready": not_ready,
                "merged": await service.store.get_state(MERGED_KEY.format(repo=repo))
                or [],
            }

        shared = await request.app.state.recent.shared(("merge-view", repo), compute)
        return {
            **shared,
            "can_arrange": await shows_maintainer(service, user, repo),
            "signed_in": await service.can_post_as(user.login),
            "scope_request": await _scope_view(service, user),
        }

    @r.post("/{repo:path}/schedule")
    async def schedule(
        request: Request,
        repo: str,
        body: ScheduleBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Schedule a PR as the maintainer who pressed it: that arms its merge at its
        head and approves the steps the schedule takes for it. One that does not meet
        the repository's target yet is scheduled only with ``force`` (by hand): its
        CI runs on its turn, and it merges once the merge rules hold."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None or not profile.merge.enabled:
            raise HTTPException(409, "merge scheduling is off for this repository")
        if await service.store.get_arm(repo, body.number) is not None:
            raise HTTPException(409, f"#{body.number} is already scheduled")
        pr = await service.a.actuator_reader.get_pr(repo, body.number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(409, f"#{body.number} is not open")
        info = await service.a.forge.get_repo(repo)
        back = holding_back(
            await gate_of(service, profile, info, pr, None), ready_conditions(profile)
        )
        if back and not body.force:
            raise HTTPException(
                409,
                f"#{body.number} is not ready: "
                + "; ".join(_item(i.name, i.detail) for i in back),
            )
        perm = await service.a.forge.get_permission(repo, user.login)
        now = service.clock.now()
        arm = MergeArm(
            repo=repo,
            number=body.number,
            sha=pr.head_sha,
            armed_by=user.login,
            armed_at=now,
            armer_permission=perm,
            position=next_position(await service.store.list_arms(repo), now),
            forced=bool(back),
        )
        await service.store.save_arm(arm)
        await service.store.set_state(skip_key(repo, body.number), None)
        await service.bus.publish(
            Event(
                kind=EventKind.MERGE_ARMED,
                repo=repo,
                number=body.number,
                sha=pr.head_sha,
                payload={"by": user.login, "armed_at": now.isoformat()},
            )
        )
        return arm.model_dump(mode="json")

    @r.put("/{repo:path}/order")
    async def reorder(
        request: Request,
        repo: str,
        body: OrderBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Put the scheduled PRs in ``numbers``' order; any left out keep theirs,
        after them. A PR already running keeps its turn."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        arms = ordered(await service.store.list_arms(repo))
        by_number = {a.number: a for a in arms}
        wanted = [n for n in dict.fromkeys(body.numbers) if n in by_number]
        rest = [a.number for a in arms if a.number not in wanted]
        base = min((position(a) for a in arms), default=0.0)
        for i, n in enumerate([*wanted, *rest]):
            await service.store.save_arm(
                by_number[n].model_copy(update={"position": base + i})
            )
        for n in [*wanted, *rest]:
            await service.bus.publish(
                Event(kind=EventKind.MERGE_QUEUE, repo=repo, number=n)
            )
        return {"order": [*wanted, *rest]}

    @r.delete("/{repo:path}/{number:int}")
    async def unschedule(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Take a PR off the schedule; automatic scheduling leaves its head alone."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        arm = await service.store.get_arm(repo, number)
        if arm is None:
            raise HTTPException(404, f"#{number} is not scheduled")
        await service.store.delete_arm(repo, number)
        await service.store.set_state(skip_key(repo, number), arm.sha)
        await service.bus.publish(
            Event(
                kind=EventKind.MERGE_DISARMED,
                repo=repo,
                number=number,
                payload={"by": user.login},
            )
        )
        cancelled = None
        profile = await service.profiles.active(repo)
        pr = await service.store.get_snapshot(repo, number)
        if profile is not None and pr is not None:
            info = await service.view_repo(repo)
            holders = [
                a.number
                for a in ordered(await service.store.list_arms(repo))
                if (await _state(service, repo, a.number)).phase in ACTIVE
            ]
            if serial(info, profile) and holders:
                # Its CI cannot count once #holder merges, and holds its machines.
                cancelled = await _cancel_ci(
                    service,
                    user,
                    pr,
                    f"unscheduled by {user.login} while #{holders[0]} holds the "
                    "merge schedule's turn",
                )
        return {"scheduled": False, "cancelled": cancelled}

    @r.post("/{repo:path}/{number:int}/cancel-ci")
    async def cancel_ci(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Cancel a PR's unfinished workflow runs, so they leave the CI machines to
        the scheduled PR whose turn it is. The PR holding the turn is the
        schedule's: unschedule it instead."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        if (
            await service.store.get_arm(repo, number) is not None
            and (await _state(service, repo, number)).phase in ACTIVE
        ):
            raise HTTPException(
                409, f"#{number} holds the schedule's turn: unschedule it instead"
            )
        pr = await service.a.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(404, f"#{number} is not an open PR")
        out = await _cancel_ci(
            service, user, pr, f"cancelled by {user.login} from the Merge page"
        )
        if out is None:
            raise HTTPException(409, f"#{number} has no CI running")
        return out

    async def _send_back(
        service: Service, repo: str, number: int, user: User, why: str
    ) -> dict[str, Any]:
        state = await _state(service, repo, number)
        arm = await service.store.get_arm(repo, number)
        if state.phase == "failed" and arm is not None:
            # Back from the failure lane, after every PR scheduled meanwhile.
            others = [
                a for a in await service.store.list_arms(repo) if a.number != number
            ]
            await service.store.save_arm(
                arm.model_copy(
                    update={"position": next_position(others, service.clock.now())}
                )
            )
        state.phase = "queued"
        state.detail = why
        state.retry = True
        state.retries += 1
        state.updates = 0
        state.started_at = None
        state.running_since = None
        state.looked_into = []
        state.finding = ""
        state.not_before = None
        await service.store.set_state(
            state_key(repo, number), state.model_dump(mode="json")
        )
        # The schedule tries its resolution of the PR's conflicts, and its fix of
        # its static checks, again.
        await service.store.set_state(resolve_key(repo, number), None)
        await service.store.set_state(fix_key(repo, number), None)
        await service.bus.publish(
            Event(kind=EventKind.MERGE_QUEUE, repo=repo, number=number)
        )
        return {"phase": state.phase}

    @r.post("/{repo:path}/{number:int}/retry")
    async def retry(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Send a PR from the failure lane back into the order: on its turn it runs
        what failed again, past the rerun budget, and its base merges count afresh."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        if await service.store.get_arm(repo, number) is None:
            raise HTTPException(404, f"#{number} is not scheduled")
        return await _send_back(
            service,
            repo,
            number,
            user,
            f"sent back by {user.login} to run its CI again",
        )

    @r.post("/{repo:path}/{number:int}/flaky")
    async def flaky(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Mark the failures that sent a PR to the failure lane flaky, as the
        maintainer who pressed it, and send it back to run them again. The verdicts
        teach the CI doctor (``ci_doctor.first_looks``, ``playbooks/flake_learn.py``)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        if await service.store.get_arm(repo, number) is None:
            raise HTTPException(404, f"#{number} is not scheduled")
        pr = await service.store.get_snapshot(repo, number)
        state = await _state(service, repo, number)
        marked = 0
        if pr is not None:
            for o in await service.store.list_ci_observations(repo, sha=pr.head_sha):
                if (
                    o.number == number
                    and o.verdict is None
                    and o.check_name in state.failed_jobs
                ):
                    await _judge(service, repo, o.id, "flaky", user.login)
                    marked += 1
        out = await _send_back(
            service, repo, number, user, f"{user.login} marked its failures flaky"
        )
        return {**out, "marked": marked}

    @r.put("/{repo:path}/verdicts/{obs_id}")
    async def verdict(
        request: Request,
        repo: str,
        obs_id: str,
        body: VerdictBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Record a person's word on a CI failure: flaky, real, or none. The same
        failure is taken that way after, and the CI doctor learns from it."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        obs = await _judge(service, repo, obs_id, body.verdict, user.login)
        return obs.model_dump(mode="json")

    @r.delete("/{repo:path}/suggestions/{sid}")
    async def dismiss_suggestion(
        request: Request, repo: str, sid: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Drop a flake pattern learned from a verdict without adding it."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        key = SUGGESTIONS.format(repo=repo)
        kept = [
            k for k in await service.store.get_state(key) or [] if k.get("id") != sid
        ]
        await service.store.set_state(key, kept)
        return {"suggestions": kept}

    @r.put("/{repo:path}/settings")
    async def settings(
        request: Request,
        repo: str,
        body: SettingsBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Turn merge scheduling or automatic scheduling on or off, or set how many
        PRs run their CI at once: a profile change, through the gate. Automatic
        scheduling schedules as whoever turns it on."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)

        def change(layer: dict[str, Any]) -> None:
            merge = _section(layer, "merge")
            plan = _section(merge, "schedule")
            if body.enabled is not None:
                merge["enabled"] = body.enabled
            if body.auto is not None:
                plan["auto"] = body.auto
                if body.auto:
                    plan["auto_by"] = user.login
                else:
                    plan.pop("auto_by", None)
            if body.parallel is not None:
                plan["parallel"] = body.parallel
            if not plan:
                merge.pop("schedule", None)

        parts = [
            f"merge scheduling {'on' if body.enabled else 'off'}"
            if body.enabled is not None
            else "",
            f"automatic scheduling {'on as ' + user.login if body.auto else 'off'}"
            if body.auto is not None
            else "",
            f"{body.parallel} at once" if body.parallel is not None else "",
        ]
        out = await _save_layer(
            service, user, repo, change, "merge: " + ", ".join(p for p in parts if p)
        )
        if out["applied"] and body.auto:
            for pr in await service.store.list_snapshots(repo):
                await service.bus.publish(
                    Event(
                        kind=EventKind.DASHBOARD_COMMAND,
                        repo=repo,
                        number=pr.number,
                        payload={"command": AUTO_COMMAND, "by": user.login},
                    )
                )
        return out

    @r.put("/{repo:path}/merging")
    async def merging(
        request: Request,
        repo: str,
        body: MergingBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Save how the repository's scheduled PRs merge: the method, the commit
        message, merging the base in, the workflows that must pass, the approvals
        the repository asks for, who may schedule, freezes, and which conflicts the
        schedule resolves by itself. A profile change, through the gate."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        info = await service.view_repo(repo)
        if body.method and info.merge_methods and body.method not in info.merge_methods:
            raise HTTPException(422, f"{repo} does not allow {body.method} merges")
        if body.subject is not None:
            try:
                body.subject.format(title="t", number=1)
            except (KeyError, IndexError, ValueError) as e:
                raise HTTPException(
                    422, "the subject may name only {title} and {number}"
                ) from e
        bad = [t for t in body.preserve_trailers or [] if not TRAILER.match(t)]
        if bad:
            raise HTTPException(422, f"not trailer names: {', '.join(bad)}")
        if any(f.end <= f.start for f in body.freezes or []):
            raise HTTPException(422, "a freeze must end after it starts")

        def change(layer: dict[str, Any]) -> None:
            merge = _section(layer, "merge")
            if body.enabled is not None:
                merge["enabled"] = body.enabled
            if body.method is not None:
                merge["method"] = body.method
            message = _section(merge, "message")
            if body.subject is not None:
                message["subject"] = body.subject.strip() or "{title} (#{number})"
            if body.preserve_trailers is not None:
                message["preserve_trailers"] = list(
                    dict.fromkeys(body.preserve_trailers)
                )
            if body.signoff_from_authors is not None:
                message["signoff_from_authors"] = body.signoff_from_authors
            if not message:
                merge.pop("message", None)
            if body.update_branch is not None:
                merge["update_branch"] = body.update_branch
            if body.parallel is not None:
                _section(merge, "schedule")["parallel"] = body.parallel
            if body.approvals is not None:
                merge["approvals"] = body.approvals
            if body.arming_who is not None:
                _section(merge, "arming")["who"] = body.arming_who
            if body.freezes is not None:
                merge["freezes"] = [f.model_dump(mode="json") for f in body.freezes]
            if body.resolve is not None:
                _section(merge, "schedule")["resolve"] = body.resolve.model_dump(
                    mode="json"
                )
            if body.fix is not None:
                _section(merge, "schedule")["fix"] = body.fix.model_dump(mode="json")
            ci = _section(layer, "ci")
            if body.required_workflows is not None:
                ci["required_workflows"] = [
                    w.strip()
                    for w in dict.fromkeys(body.required_workflows)
                    if w.strip()
                ]
            if body.cancelled_is_failure is not None:
                ci["cancelled_is_failure"] = body.cancelled_is_failure
            if not ci:
                layer.pop("ci", None)

        # ``change`` runs once, inside ``_save_layer``; the summary is built after.
        return await _save_layer(service, user, repo, change, "merge: how PRs merge")

    @r.post("/{repo:path}/{number:int}/check")
    async def check_one(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Read a scheduled PR from GitHub again now and have the schedule look at
        its step again (its CI, approvals, and whether it may merge)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        if await service.store.get_arm(repo, number) is None:
            raise HTTPException(404, f"#{number} is not scheduled")
        await service.check_scheduled(repo, number)
        return {"checked": number}

    @r.put("/{repo:path}/ci-start")
    async def ci_start(
        request: Request,
        repo: str,
        body: CiStartBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Save how the repository's CI starts (``ci.start``, the ``ci_gate`` label)
        and its merge notes: a profile change, through the gate."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        info = await service.view_repo(repo)
        label = (body.gate_label or "").strip()
        if label and label not in info.labels:
            raise HTTPException(422, f"{repo} has no label {label!r}")

        def change(layer: dict[str, Any]) -> None:
            start = _section(layer, "ci", "start")
            start.update(
                {
                    "how": body.how.strip(),
                    "gate_jobs": sorted(
                        {j.strip() for j in body.gate_jobs if j.strip()}
                    ),
                    "summary_jobs": sorted(
                        {j.strip() for j in body.summary_jobs if j.strip()}
                    ),
                    "start_minutes": body.start_minutes,
                    "run_minutes": body.run_minutes,
                    "optional_checks": sorted(
                        {c.strip() for c in body.optional_checks if c.strip()}
                    ),
                }
            )
            if body.ready is not None:
                _section(layer, "merge", "schedule")["ready"] = list(
                    dict.fromkeys(body.ready)
                )
            if body.gate_label is not None:
                roles = _section(layer, "labels", "roles")
                if label:
                    roles["ci_gate"] = label
                else:
                    roles.pop("ci_gate", None)
            if body.merge_notes is not None:
                _section(layer, "merge", "schedule")["notes"] = body.merge_notes.strip()
            if body.approvals:
                merge = _section(layer, "merge")
                merge["approvals"] = max(
                    int(merge.get("approvals") or 0), body.approvals
                )

        return await _save_layer(service, user, repo, change, "ci: how CI starts")

    @r.put("/{repo:path}/ready")
    async def set_ready(
        request: Request,
        repo: str,
        body: ReadyBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Set what a PR must meet to be scheduled (``merge.schedule.ready``): a
        profile change, through the gate. ``null`` goes back to every condition the
        merge rules hold a PR to."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)

        def change(layer: dict[str, Any]) -> None:
            plan = _section(layer, "merge", "schedule")
            if body.ready is None:
                plan.pop("ready", None)
            else:
                plan["ready"] = list(dict.fromkeys(body.ready))

        return await _save_layer(
            service,
            user,
            repo,
            change,
            "merge: schedulable when "
            + (
                ", ".join(body.ready)
                if body.ready is not None
                else "the merge rules hold"
            ),
        )

    @r.put("/{repo:path}/blocking")
    async def set_blocking(
        request: Request,
        repo: str,
        body: BlockingBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Set which labels hold a PR's merge back (``merge.blocking_roles``, by
        their roles): a profile change, through the gate."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        unknown = [r for r in body.roles if r not in profile.labels.roles]
        if unknown:
            raise HTTPException(
                422, f"{repo} gives no label to {', '.join(sorted(unknown))}"
            )
        roles = list(dict.fromkeys(body.roles))

        def change(layer: dict[str, Any]) -> None:
            _section(layer, "merge")["blocking_roles"] = roles

        labels = [profile.labels.roles[r] for r in roles]
        return await _save_layer(
            service,
            user,
            repo,
            change,
            "merge: held back by " + (", ".join(labels) if labels else "no label"),
        )

    @r.post("/{repo:path}/check")
    async def check(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Read every open PR of ``repo`` from GitHub again now, in full, in the
        background (``Service.check_prs``); the page updates as each is read."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        record = await service.store.get_repo(repo)
        if record is None or not record.active or not service.serves(repo):
            raise HTTPException(
                409, f"the service does not read {repo} until its profile is approved"
            )
        started = service.check_prs(repo)
        return {
            "started": started,
            "prs": len(await service.store.list_snapshots(repo)),
        }

    @r.post("/{repo:path}/describe")
    async def describe(
        request: Request,
        repo: str,
        body: DescribeBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent turn the rules, in the maintainer's own words, into a draft
        of the settings, changing only what they spoke to; the draft waits on the
        page for a maintainer to save."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            payload={"command": CI_RULES, "by": user.login, "text": body.text},
        )
        await service.bus.publish(event)
        return {"event": event.id}

    @r.post("/{repo:path}/draft/apply")
    async def apply_draft(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Save the agent's draft as it stands, every setting in it: a profile
        change, through the gate. The draft was checked against what the repository
        has when it was made (``ci_rules.checked``)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        stored = await service.store.get_state(DRAFT_KEY.format(repo=repo))
        if profile is None or not isinstance(stored, dict):
            raise HTTPException(404, "there is no draft to save")
        draft = draft_of(stored, profile)
        own = await _own_pattern_ids(service, repo)
        inherited = {
            s_.id: s_ for s_ in profile.ci.flake_signatures if s_.id not in own
        }
        out = await _save_layer(
            service,
            user,
            repo,
            lambda layer: _apply_draft(layer, draft, inherited),
            "merge and CI rules, from "
            + (
                "their words" if stored.get("source") == "words" else "an investigation"
            ),
        )
        if out["applied"]:
            await service.store.set_state(DRAFT_KEY.format(repo=repo), None)
        return out

    @r.delete("/{repo:path}/draft")
    async def discard_draft(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        await service.store.set_state(DRAFT_KEY.format(repo=repo), None)
        return {"draft": None}

    @r.put("/{repo:path}/flaky")
    async def set_flaky(
        request: Request,
        repo: str,
        body: FlakyBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Set how failed CI is judged and run again: a profile change, through the
        gate."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)

        def change(layer: dict[str, Any]) -> None:
            ci = _section(layer, "ci")
            ci["diagnose"] = body.diagnose
            ci["root_cause"] = body.root_cause
            rerun = _section(ci, "rerun")
            rerun.update(
                {
                    "max_per_sha": body.max_reruns,
                    "backoff_seconds": body.backoff_seconds,
                    "unknown": body.rerun_unknown,
                }
            )

        return await _save_layer(
            service,
            user,
            repo,
            change,
            f"ci: flakes judged by {body.diagnose}, rerun up to {body.max_reruns} times",
        )

    @r.put("/{repo:path}/flake-patterns")
    async def set_patterns(
        request: Request,
        repo: str,
        body: PatternsBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Set the repository's own flake patterns (a pack's stay): a profile
        change, through the gate."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        ids = [p.id for p in body.patterns]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, "each flake pattern needs an id of its own")
        for p in body.patterns:
            try:
                re.compile(p.pattern)
            except re.error as e:
                raise HTTPException(
                    422, f"pattern {p.id!r} is not a regular expression: {e}"
                ) from e

        def change(layer: dict[str, Any]) -> None:
            ci = _section(layer, "ci")
            if body.patterns:
                ci["flake_signatures"] = [
                    p.model_dump(mode="json") for p in body.patterns
                ]
            else:
                ci.pop("flake_signatures", None)

        return await _save_layer(
            service, user, repo, change, f"ci: {len(body.patterns)} own flake patterns"
        )

    @r.post("/{repo:path}/scope")
    async def request_scope(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Ask for the workflow scope on the service's sign-in, as its owner: a
        one-time code to enter on the forge as the service's account."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        decision = await service.service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        try:
            await service.request_scopes(repo, ["workflow"], user.login)
        except Exception as e:
            raise HTTPException(409, str(e)) from e
        return await _scope_view(service, user) or {}

    @r.post("/{repo:path}/investigate")
    async def investigate(
        request: Request,
        repo: str,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent read the repository's workflows and contribution guide and
        draft how its CI starts; the draft waits on the page for a maintainer."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            payload={"command": CI_RULES, "by": user.login},
        )
        await service.bus.publish(event)
        return {"event": event.id}

    return r
