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

"""Agents' work as people follow it: each job that runs an agent, live (design 5.13).

A job's view has who asked for it, which agent runs it, the agent's steps so far, the
questions it waits on, and what it produced (proposals and actions). Viewing needs
write access to the job's repository, like every other page; stopping a job needs to
be the person who asked for it or a maintainer of the repository, checked live.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ProposalState
from ..domain.agents import QuestionState
from ..domain.events import Event, EventKind
from ..domain.ids import check_repo
from ..domain.labs import unusable
from ..domain.memory import profile_path
from ..domain.permissions import Permission
from ..domain.profile import LabBinding
from ..domain.records import Job, JobStatus
from ..profiles.layers import ProfileError, parse_yaml
from ..service import Service
from .api import require_view, service_of, visible_repos
from .auth import User, current_user

AGENT_PLAYBOOKS = {
    "review": "review",
    "followup": "followup",
    "fix": "fix",
    "reproduce": "reproduce",
    "onboard": "onboard",
    "self_improve": "self_improve",
}
"""Playbooks whose work is an agent's, and the task kind of their main agent run."""

ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING)


def _payload(job: Job) -> dict[str, Any]:
    first = job.events[0] if job.events else {}
    return (first.get("payload") if isinstance(first, dict) else None) or {}


async def _default_agent(service: Service, job: Job) -> str | None:
    kind = AGENT_PLAYBOOKS.get(job.playbook)
    if kind is None:
        return None
    profile = await service.profiles.active(job.repo) if job.repo else None
    route = (
        profile.route(kind, service.info.routing)
        if profile
        else service.info.routing.get(kind)
    )
    return route.primary if route else None


async def job_view(
    service: Service, job: Job, *, steps: bool = False, me: str | None = None
) -> dict[str, Any]:
    """What the dashboard shows about ``job``."""
    payload = _payload(job)
    by = payload.get("by")
    found = await service.progress.steps(job.id)
    ran = next((s.runtime for s in found if s.kind == "start"), None)
    questions = [
        q
        for q in await service.store.list_questions(QuestionState.PENDING, job.repo)
        if q.job_id == job.id
    ]
    produced = []
    if job.repo:
        pending = {
            p.action_id: p
            for p in await service.store.list_proposals(ProposalState.PENDING, job.repo)
        }
        for record in await service.store.list_actions(
            repo=job.repo, number=job.number, limit=200
        ):
            if record.action.provenance.job_id != job.id:
                continue
            proposal = pending.get(record.action.id)
            produced.append(
                {
                    "id": record.action.id,
                    "kind": record.action.kind,
                    "status": record.status,
                    "proposal": proposal.id if proposal else None,
                    "for": record.action.draft_for,
                }
            )
    pr = (
        await service.store.get_snapshot(job.repo, job.number)
        if job.repo and job.number
        else None
    )
    out: dict[str, Any] = {
        "id": job.id,
        "playbook": job.playbook,
        "status": job.status,
        "repo": job.repo,
        "number": job.number,
        "title": pr.title if pr else None,
        "by": by,
        "mine": bool(me and by and by.lower() == me.lower()),
        "agent": payload.get("agent") or ran or await _default_agent(service, job),
        "note": payload.get("note") or None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "error": job.error,
        "result": job.result,
        "wait": job.wait.reason if job.wait else None,
        "questions": [q.id for q in questions],
        "questions_for_me": sum(
            1
            for q in questions
            if me and (q.asked_for or "").lower() in ("", me.lower())
        ),
        "produced": produced,
        "last_step": found[-1].model_dump(mode="json") if found else None,
    }
    if steps:
        out["steps"] = [s.model_dump(mode="json") for s in found]
    return out


async def _job(service: Service, user: User, job_id: str) -> Job:
    """The job ``job_id`` names: a job id, or the id of the request event that
    started it (what the dashboard knows right after asking for the work)."""
    job = await service.store.get_job(job_id)
    if job is None and job_id.startswith("evt_"):
        job = next(
            (
                j
                for j in await service.store.list_jobs(limit=500)
                if j.events and j.events[0].get("id") == job_id
            ),
            None,
        )
    if job is None:
        raise HTTPException(404, "no such job yet")
    if job.repo:
        await require_view(service, user, job.repo)
    elif (
        user.login not in service.settings.operators
        and (_payload(job).get("by") or "").lower() != user.login.lower()
    ):
        raise HTTPException(
            403, "only operators and whoever asked see jobs outside repositories"
        )
    return job


class MachineUseBody(BaseModel):
    """How a repository uses a test machine (its profile's ``labs.<name>``)."""

    use: bool = True
    setup: str = Field(default="", max_length=2000)
    timeout_seconds: int | None = Field(default=None, ge=60, le=6 * 3600)
    forbid: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)


class ReproduceBody(BaseModel):
    lab: str
    note: str = Field(default="", max_length=2000)
    """What to check, when the PR does not make it obvious."""
    agent: str | None = None


def _machine_entry(entry: dict[str, Any], body: MachineUseBody) -> dict[str, Any]:
    """``labs.<machine>`` of a settings layer with ``body`` applied; keys the body
    leaves empty are removed, and keys it does not cover (template paths) are kept."""
    entry["use"] = body.use
    setup = body.setup.strip()
    if setup:
        entry["setup"] = setup
    else:
        entry.pop("setup", None)
    if body.timeout_seconds:
        entry["timeout_seconds"] = body.timeout_seconds
    else:
        entry.pop("timeout_seconds", None)
    guard = dict(entry.get("guard") or {})
    forbid = [f.strip() for f in body.forbid if f.strip()]
    if forbid:
        guard["forbid"] = forbid
    else:
        guard.pop("forbid", None)
    if guard:
        entry["guard"] = guard
    else:
        entry.pop("guard", None)
    paths = [x.strip() for x in body.protected_paths if x.strip()]
    if paths:
        entry["protected_paths"] = paths
    else:
        entry.pop("protected_paths", None)
    return entry


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/labs")
    async def labs(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The test machines offered to ``repo``, each with whether the repository
        uses it and how; and whether the user may change that."""
        service = service_of(request)
        await require_view(service, user, check_repo(repo))
        profile = await service.profiles.active(repo)
        gate = (
            await service.gate.check(repo, user.login, profile, person=True)
            if profile
            else None
        )
        out = []
        for name, machine in sorted(service.machines.items()):
            if not machine.serves(repo):
                continue
            binding = profile.labs.get(name) if profile else None
            out.append(
                {
                    "name": name,
                    "description": machine.description,
                    "transport": machine.transport,
                    "machine_setup": machine.setup,
                    "machine_timeout_seconds": machine.timeout_seconds,
                    "used": binding is not None and binding.use,
                    "setup": (binding.setup or "") if binding else "",
                    "timeout_seconds": binding.timeout_seconds if binding else None,
                    "forbid": binding.guard.forbid if binding else [],
                    "protected_paths": binding.protected_paths if binding else [],
                }
            )
        return {
            "repo": repo,
            "machines": out,
            "can_configure": bool(gate and gate.allowed),
            "reason": gate.reason if gate else f"{repo} has no approved profile yet",
        }

    @r.put("/repos/{owner}/{name}/labs/{lab}")
    async def use_machine(
        request: Request,
        owner: str,
        name: str,
        lab: str,
        body: MachineUseBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Change how the repository uses a test machine, in its settings layer: a
        maintainer of the repository applies it by making it, anyone else proposes
        it."""
        service = service_of(request)
        repo = check_repo(f"{owner}/{name}")
        await require_view(service, user, repo)
        machine = service.machines.get(lab)
        if machine is None or not machine.serves(repo):
            raise HTTPException(404, f"there is no test machine {lab!r} for {repo}")
        current = await service.profiles.active(repo)
        if current is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        layer = parse_yaml(
            await service.a.memory.read_text(profile_path(repo)) or "", "settings"
        )
        labs = dict(layer.get("labs") or {})
        for pattern in body.forbid:
            try:
                re.compile(pattern)
            except re.error as e:
                raise HTTPException(
                    422, f"{pattern!r} is not a regular expression ({e})"
                ) from e
        entry = _machine_entry(dict(labs.get(lab) or {}), body)
        try:
            LabBinding.model_validate(entry)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        labs[lab] = entry
        layer["labs"] = labs
        what = f"labs: {'use' if body.use else 'stop using'} {lab}"
        try:
            merged, change = await service.profiles.propose_memory_layer(
                repo, layer, user.login, what
            )
        except ProfileError as e:
            raise HTTPException(422, str(e)) from e
        decision = await service.gate.check(
            repo, user.login, current, merged.profile, person=True
        )
        if decision.allowed:
            change.approvals = {repo: [user.login]}
        change.requested_by = user.login
        await service.store.save_memory_change(change)
        await service.submit_memory_change(change)
        stored = await service.store.get_memory_change(change.id)
        return {
            "applied": bool(stored and stored.state == "merged"),
            "change": stored.model_dump(mode="json") if stored else None,
            "reason": decision.reason,
        }

    @r.post("/prs/{owner}/{name}/{number}/reproduce")
    async def reproduce(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: ReproduceBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent write a reproduction for the PR; the script comes back to the
        requester to approve before it runs on the machine."""
        service = service_of(request)
        repo = check_repo(f"{owner}/{name}")
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        why = unusable(
            repo,
            body.lab,
            service.machines.get(body.lab),
            profile.labs.get(body.lab) if profile else None,
        )
        if why:
            raise HTTPException(409, why)
        pr = await service.store.get_snapshot(repo, number)
        if pr is None:
            raise HTTPException(404, f"{repo}#{number} is not known yet")
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            sha=pr.head_sha,
            payload={
                "command": "reproduce",
                "by": user.login,
                "lab": body.lab,
                **({"agent": body.agent} if body.agent else {}),
                **({"note": body.note} if body.note else {}),
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"repo": repo, "number": number, "event": event.id, "lab": body.lab}

    @r.get("/work")
    async def work(
        request: Request,
        everyone: bool = False,
        days: int = 7,
        repo: str | None = None,
        number: int | None = None,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        """Agent jobs in the user's repositories (or in ``repo``, on PR ``number``):
        running ones, then those that ended in the last ``days``, newest first; only
        the user's own unless ``everyone``."""
        service = service_of(request)
        if repo is not None:
            await require_view(service, user, check_repo(repo))
            visible = {repo}
        else:
            visible = set(await visible_repos(service, user))
        since = service.clock.now() - timedelta(days=max(1, min(days, 90)))
        out = []
        for job in await service.store.list_jobs(repo=repo, limit=500):
            if job.playbook not in AGENT_PLAYBOOKS or job.repo not in visible:
                continue
            if number is not None and job.number != number:
                continue
            if job.status not in ACTIVE and job.updated_at < since:
                continue
            by = (_payload(job).get("by") or "").lower()
            if not everyone and by != user.login.lower():
                continue
            view = await job_view(service, job, me=user.login)
            if (
                job.status not in ACTIVE
                and (job.result or {}).get("skipped")
                and view["last_step"] is None
            ):
                # Nothing ran: another playbook took the same request (a follow-up
                # for a PR that has no review yet), so there is no work to show.
                continue
            out.append(view)
        out.sort(key=lambda j: (j["status"] in ACTIVE, j["updated_at"]), reverse=True)
        return out[:100]

    @r.get("/jobs/{job_id}")
    async def get_job(
        request: Request, job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        job = await _job(service, user, job_id)
        return await job_view(service, job, steps=True, me=user.login)

    @r.post("/jobs/{job_id}/stop")
    async def stop_job(
        request: Request, job_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        job = await _job(service, user, job_id)
        by = (_payload(job).get("by") or "").lower()
        if by != user.login.lower():
            allowed = (
                (await service.a.forge.get_permission(job.repo, user.login)).at_least(
                    Permission.MAINTAIN
                )
                if job.repo
                else user.login in service.settings.operators
            )
            if not allowed:
                raise HTTPException(
                    403,
                    "only the person who asked for this job or a maintainer of the "
                    "repository can stop it",
                )
        stopped = await service.engine.stop_job(job.id, user.login)
        assert stopped is not None
        return await job_view(service, stopped, steps=True, me=user.login)

    return r
