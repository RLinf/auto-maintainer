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

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import ProposalState
from ..domain.agents import QuestionState
from ..domain.events import Event, EventKind
from ..domain.ids import check_repo
from ..domain.permissions import Permission
from ..domain.records import Job, JobStatus
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
    elif user.login not in service.settings.operators:
        raise HTTPException(403, "only operators see jobs outside repositories")
    return job


class ReproduceBody(BaseModel):
    lab: str
    note: str = Field(default="", max_length=2000)
    """What to check, when the PR does not make it obvious."""
    agent: str | None = None


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/labs")
    async def labs(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """The test machines ``repo`` may use, for the PR page's Reproduce button."""
        service = service_of(request)
        await require_view(service, user, check_repo(repo))
        return [
            {
                "name": name,
                "description": machine.description,
                "transport": machine.transport,
            }
            for name, machine in sorted(service.machines.items())
            if machine.serves(repo)
        ]

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
        machine = service.machines.get(body.lab)
        if machine is None or not machine.serves(repo):
            raise HTTPException(
                404, f"there is no test machine {body.lab!r} for {repo}"
            )
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
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        """Agent jobs in the user's repositories: running ones, then those that ended
        in the last ``days``, newest first; only the user's own unless ``everyone``."""
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        since = service.clock.now() - timedelta(days=max(1, min(days, 90)))
        out = []
        for job in await service.store.list_jobs(limit=500):
            if job.playbook not in AGENT_PLAYBOOKS or job.repo not in visible:
                continue
            if job.status not in ACTIVE and job.updated_at < since:
                continue
            by = (_payload(job).get("by") or "").lower()
            if not everyone and by != user.login.lower():
                continue
            out.append(await job_view(service, job, me=user.login))
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
