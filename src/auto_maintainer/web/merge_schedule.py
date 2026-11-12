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

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.memory import profile_path
from ..domain.merge_gate import GateResult, MergeArm, evaluate_merge_gate
from ..domain.merge_schedule import (
    ScheduleState,
    holding_back,
    next_position,
    ordered,
    position,
    ready_conditions,
    serial,
    skip_key,
    state_key,
)
from ..domain.profile import ReadyCondition, RepoProfile
from ..domain.run_ci import ci_view
from ..domain.snapshot import Mergeable, PrSnapshot, PrState, RepoInfo
from ..playbooks.auto_schedule import AUTO_COMMAND
from ..playbooks.ci_rules import COMMAND as CI_RULES
from ..playbooks.ci_rules import DRAFT_KEY
from ..playbooks.hygiene import hygiene_key
from ..playbooks.merge import MERGED_KEY
from ..profiles.layers import ProfileError, parse_yaml
from ..service import Service
from .api import _repo, is_maintainer, permission, require_view, service_of
from .auth import User, current_user


class ScheduleBody(BaseModel):
    number: int = Field(ge=1)


class OrderBody(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=500)


class SettingsBody(BaseModel):
    enabled: bool | None = None
    auto: bool | None = None
    parallel: int | None = Field(default=None, ge=1, le=20)


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


class ReadyBody(BaseModel):
    ready: list[ReadyCondition] | None
    """``None`` for every condition the merge rules hold a PR to."""


async def gate_of(
    service: Service,
    profile: RepoProfile,
    info: RepoInfo,
    pr: PrSnapshot,
    arm: MergeArm | None,
) -> GateResult:
    """The merge gate for ``pr`` as the merge job reads it, from what the service
    has: reviewers' permissions cached for the views, the hygiene results kept."""
    perms = {
        r.author: await permission(service, User(login=r.author), pr.repo)
        for r in pr.reviews
    }
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


def _section(layer: dict[str, Any], *path: str) -> dict[str, Any]:
    node = layer
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    return node


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
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        info = await service.view_repo(repo)
        arms = ordered(await service.store.list_arms(repo))
        armed = {a.number: a for a in arms}
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
                "since": state.since.isoformat() if state.since else None,
                "scheduled_by": arm.armed_by,
                "auto": arm.auto,
                "failed_jobs": state.failed_jobs,
                "causes": state.causes,
                "finding": state.finding,
                "approval_needed": state.approval_needed,
                "stale": not same_commit(arm.sha, pr.head_sha),
            }
            (failed if state.phase == "failed" else scheduled).append(row)
        candidates, not_ready = [], []
        for pr in sorted(snaps.values(), key=lambda p: p.number):
            if pr.number in armed or pr.state != PrState.OPEN:
                continue
            if pr.is_draft and not profile.scope.include_drafts:
                continue
            gate = await gate_of(service, profile, info, pr, None)
            back = holding_back(gate, ready_conditions(profile))
            row = _row(pr, profile)
            if not back:
                candidates.append(row)
            else:
                row["reasons"] = [_item(i.name, i.detail) for i in back[:3]]
                row["conflict"] = pr.mergeable == Mergeable.CONFLICTING
                not_ready.append(row)
        draft = await service.store.get_state(DRAFT_KEY.format(repo=repo))
        start = profile.ci.start
        return {
            "repo": repo,
            "enabled": profile.merge.enabled,
            "can_arrange": await is_maintainer(service, user, repo),
            "signed_in": await service.can_post_as(user.login),
            "mode": "serial" if serial(info, profile) else "parallel",
            "parallel": profile.merge.schedule.parallel,
            "auto": profile.merge.schedule.auto,
            "auto_by": profile.merge.schedule.auto_by,
            "paused": await service.pipeline.paused(repo),
            "rules": {
                "approvals": max(profile.merge.approvals, info.required_approvals or 0),
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
                "hygiene": [s.id for s in profile.checks if s.blocking and s.enabled],
                "ready": list(ready_conditions(profile)),
                "ready_set": profile.merge.schedule.ready is not None,
            },
            "ci_start": {
                **start.model_dump(mode="json"),
                "gate_label": profile.label_for("ci_gate"),
                "reruns": profile.ci.rerun.max_per_sha,
            },
            "draft": draft if isinstance(draft, dict) else None,
            "scheduled": scheduled,
            "failed": failed,
            "candidates": candidates,
            "not_ready": not_ready,
            "merged": await service.store.get_state(MERGED_KEY.format(repo=repo)) or [],
        }

    @r.post("/{repo:path}/schedule")
    async def schedule(
        request: Request,
        repo: str,
        body: ScheduleBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Schedule a ready PR as the maintainer who pressed it: that arms its merge
        at its head and approves the steps the schedule takes for it."""
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
        if back:
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
        return {"scheduled": False}

    @r.post("/{repo:path}/{number:int}/retry")
    async def retry(
        request: Request,
        repo: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Send a PR from the failure lane back into the order, its reruns and base
        merges counted afresh."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        await _maintainer(service, user, repo)
        if await service.store.get_arm(repo, number) is None:
            raise HTTPException(404, f"#{number} is not scheduled")
        state = await _state(service, repo, number)
        state.phase = "queued"
        state.detail = f"sent back by {user.login}"
        state.updates = 0
        state.started_at = None
        state.running_since = None
        state.looked_into = []
        state.finding = ""
        await service.store.set_state(
            state_key(repo, number), state.model_dump(mode="json")
        )
        await service.bus.publish(
            Event(kind=EventKind.MERGE_QUEUE, repo=repo, number=number)
        )
        return {"phase": state.phase}

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
