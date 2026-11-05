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

"""The dashboard API (design 5.13).

Viewing a repository needs write access to it. Changing its configuration goes
through the configuration gate, which checks the user's live permission against
the approved profile's ``access.configure``. Agents' questions about a repository
are answered by its maintainers; questions outside any repository by operators.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionKind,
    ActionStatus,
    MemoryChangePayload,
    ProposalState,
    make_action,
)
from ..domain.agents import AgentAnswer, AgentQuestion, QuestionState
from ..domain.events import Event, EventKind
from ..domain.ids import check_repo
from ..domain.memory import (
    MemoryChangeState,
    Signal,
    SignalKind,
    repo_dir,
    repos_of_path,
)
from ..domain.merge_gate import MergeArm, evaluate_merge_gate
from ..domain.permissions import Permission
from ..domain.profile import RepoProfile
from ..domain.records import AuditEntry, JobStatus
from ..domain.review import classify_review_state
from ..domain.scoring import Evaluation, recompute
from ..playbooks.evaluate import haystack, rubric_of
from ..playbooks.hygiene import hygiene_key
from ..profiles.layers import ProfileError, parse_yaml
from ..profiles.lock import profile_diff
from ..service import Service
from .auth import User, current_user

PERMISSION_TTL = timedelta(minutes=10)


def service_of(request: Request) -> Service:
    return request.app.state.service


async def permission(service: Service, user: User, repo: str) -> Permission:
    """The user's permission on ``repo``, cached for ten minutes, for viewing only."""
    now = service.clock.now()
    record = await service.store.get_user(user.login)
    from ..domain.ports import UserRecord

    if record is None:
        record = UserRecord(
            login=user.login, name=user.name, avatar_url=user.avatar_url
        )
    fresh = (
        record.permissions_checked_at
        and now - record.permissions_checked_at < PERMISSION_TTL
    )
    if fresh and repo in record.permissions:
        return Permission(record.permissions[repo])
    perm = await service.a.forge.get_permission(repo, user.login)
    record.permissions[repo] = perm.value
    record.permissions_checked_at = now
    await service.store.save_user(record)
    return perm


async def require_view(service: Service, user: User, repo: str) -> None:
    check_repo(repo)
    if not (await permission(service, user, repo)).at_least(Permission.WRITE):
        raise HTTPException(403, f"you need write access to {repo} to see it here")


async def visible_repos(service: Service, user: User) -> list[str]:
    out = []
    for record in await service.store.list_repos():
        if (await permission(service, user, record.repo)).at_least(Permission.WRITE):
            out.append(record.repo)
    return out


def _repo(owner: str, name: str) -> str:
    return check_repo(f"{owner}/{name}")


class OverrideBody(BaseModel):
    scores: dict[str, float] | None = None
    manual_boost: float | None = None
    tier: str | None = None
    reason: str


class ApproveBody(BaseModel):
    payload: dict[str, Any] | None = None


class RejectBody(BaseModel):
    reason: str


class RepoBody(BaseModel):
    repo: str


class HashBody(BaseModel):
    hash: str


class SettingsBody(BaseModel):
    layer: dict[str, Any] | None = None
    yaml: str | None = None
    title: str = "profile: update settings"


class LabBody(BaseModel):
    lab: str | None = None
    template: str
    args: dict[str, str] = Field(default_factory=dict)


class PauseBody(BaseModel):
    reason: str = ""


class AnswerBody(BaseModel):
    allow: bool = True
    option_id: str | None = None
    answers: dict[str, list[str]] = Field(default_factory=dict)
    message: str = Field(default="", max_length=4000)


async def may_answer(service: Service, user: User, question: AgentQuestion) -> bool:
    """Maintainers answer for their repositories; operators for everything else."""
    if question.repo is None:
        return user.login in service.settings.operators
    return (await permission(service, user, question.repo)).at_least(Permission.WRITE)


class PrTarget(BaseModel):
    repo: str
    number: int


class ReviewsBody(BaseModel):
    prs: list[PrTarget]


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/me")
    async def me(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        return {
            **user.model_dump(),
            "operator": user.login in service.settings.operators,
            "repos": await visible_repos(service, user),
            "dev_mode": service.settings.dev_mode,
            "paused": bool(await service.store.get_state("paused")),
            "pending_questions": len(
                [
                    q
                    for q in await service.store.list_questions(QuestionState.PENDING)
                    if await may_answer(service, user, q)
                ]
            ),
        }

    # -- repositories and profiles ------------------------------------------------------

    @r.get("/repos")
    async def repos(
        request: Request, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        out = []
        visible = set(await visible_repos(service, user))
        for record in await service.store.list_repos():
            if record.repo not in visible:
                continue
            state = await service.store.get_profile_state(record.repo)
            snapshots = await service.store.list_snapshots(record.repo)
            pending = list(
                await service.store.list_proposals(ProposalState.PENDING, record.repo)
            )
            out.append(
                {
                    **record.model_dump(mode="json"),
                    "open_prs": len(snapshots),
                    "pending_proposals": len(pending),
                    "candidate_pending": bool(state and state.candidate_hash),
                    "candidate_error": state.candidate_error if state else None,
                }
            )
        return out

    @r.post("/repos")
    async def add_repo(
        request: Request, body: RepoBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = check_repo(body.repo)
        decision = await service.gate.check(
            repo, user.login, await service.profiles.active(repo)
        )
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        try:
            record = await service.add_repo(repo, user.login)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return record.model_dump(mode="json")

    @r.get("/repos/{owner}/{name}/profile")
    async def get_profile(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        lock = await service.profiles.lock(repo)
        state = await service.profiles.refresh(repo)
        layers = await service.loader.layers(repo)
        current = await service.profiles.active(repo)
        can = await service.gate.check(repo, user.login, current)
        return {
            "lock": lock.model_dump(mode="json", by_alias=True) if lock else None,
            "candidate": state.model_dump(mode="json"),
            "layers": [
                {
                    "name": layer.name,
                    "kind": layer.kind,
                    "source": layer.source,
                    "hash": layer.hash,
                }
                for layer in layers
            ],
            "memory_layer": (
                await service.a.memory.read_text(f"{repo_dir(repo)}/profile.yaml")
            )
            or "",
            "can_configure": can.allowed,
            "gate": can.model_dump(mode="json"),
        }

    @r.post("/repos/{owner}/{name}/profile/approve")
    async def approve_profile(
        request: Request,
        owner: str,
        name: str,
        body: HashBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        try:
            decision, change = await service.profiles.approve_candidate(
                repo, user.login, body.hash
            )
        except ProfileError as e:
            raise HTTPException(409, str(e)) from e
        if change is None:
            raise HTTPException(403, decision.reason)
        await service.store.save_memory_change(change)
        record = await service.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                repo,
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            )
        )
        await service.store.add_audit(
            AuditEntry(
                kind="config",
                repo=repo,
                actor=user.login,
                summary=f"approved profile {body.hash}",
            )
        )
        await service.actuator.drain()
        stored = await service.store.get_memory_change(change.id)
        return {
            "change": stored.model_dump(mode="json") if stored else None,
            "action": record.status,
        }

    @r.put("/repos/{owner}/{name}/settings")
    async def put_settings(
        request: Request,
        owner: str,
        name: str,
        body: SettingsBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        try:
            layer = (
                body.layer
                if body.layer is not None
                else parse_yaml(body.yaml or "", "settings")
            )
            merged, change = await service.profiles.propose_memory_layer(
                repo, layer, user.login, body.title
            )
        except ProfileError as e:
            raise HTTPException(422, str(e)) from e
        current = await service.profiles.active(repo)
        decision = await service.gate.check(repo, user.login, current, merged.profile)
        if decision.allowed:
            change.approvals = {repo: [user.login]}
        await service.store.save_memory_change(change)
        await service.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                repo,
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            )
        )
        await service.actuator.drain()
        stored = await service.store.get_memory_change(change.id)
        return {
            "change": stored.model_dump(mode="json") if stored else None,
            "approved": decision.allowed,
            "gate": decision.model_dump(mode="json"),
            "diff": profile_diff(current.dump() if current else None, merged.data),
        }

    @r.get("/repos/{owner}/{name}/rubric")
    async def get_rubric(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The approved rubric and active stage goals (internal)."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(404, f"{repo} has no approved profile")
        rubric = rubric_of(profile)
        goals = await service.knowledge.stage_goals(repo, profile)
        now = service.clock.now()
        return {
            "rubric": rubric.model_dump(mode="json"),
            "tiers": rubric.tier_names(),
            "stage_goals": [
                g.model_dump(mode="json") | {"live": g.is_live(now)} for g in goals
            ],
        }

    @r.post("/repos/{owner}/{name}/pause")
    async def pause_repo(
        request: Request,
        owner: str,
        name: str,
        body: PauseBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.pipeline.set_paused(True, user.login, repo, body.reason)
        return {"paused": True}

    @r.post("/repos/{owner}/{name}/resume")
    async def resume_repo(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        decision = await service.gate.check(
            repo, user.login, await service.profiles.active(repo)
        )
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        await service.pipeline.set_paused(False, user.login, repo)
        return {"paused": False}

    @r.get("/repos/{owner}/{name}/agreement")
    async def agreement(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        return await agreement_scores(service, repo)

    # -- pull requests -------------------------------------------------------------------

    @r.get("/prs")
    async def prs(
        request: Request,
        repo: str | None = None,
        tier: str | None = None,
        state: str | None = None,
        mine: bool = False,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        repos_ = [repo] if repo else await visible_repos(service, user)
        if repo:
            await require_view(service, user, repo)
        claims = await service.store.get_claims()
        rows = []
        for name in repos_:
            profile = await service.profiles.active(name)
            if profile is None:
                continue
            ids = service.info.identities | set(profile.people.review_identities)
            activity = await review_activity(service, name)
            for pr in await service.store.list_snapshots(name):
                cls = classify_review_state(
                    pr,
                    ids,
                    profile.comments.legacy_markers,
                    profile.scope.include_drafts,
                )
                ev = await service.store.get_evaluation(name, pr.number)
                claim = claims.get(f"{name}#{pr.number}")
                if tier and (ev is None or ev.tier != tier):
                    continue
                if state and cls.state != state:
                    continue
                if (
                    mine
                    and claim != user.login
                    and user.login not in pr.requested_reviewers
                ):
                    continue
                rows.append(
                    {
                        "repo": name,
                        "number": pr.number,
                        "title": pr.title,
                        "author": pr.author,
                        "url": pr.url,
                        "head": pr.head_sha[:7],
                        "labels": pr.labels,
                        "draft": pr.is_draft,
                        "updated_at": pr.updated_at,
                        "review_state": cls.state,
                        "reviewed_sha": (cls.reviewed_sha or "")[:7],
                        "claimed_by": claim,
                        "tier": ev.tier if ev else None,
                        "total": ev.total if ev else None,
                        "summary": ev.summary if ev else "",
                        "scores": ev.scores if ev else {},
                        "review_trigger": profile.review.trigger,
                        "review_activity": activity.get(pr.number, {}),
                    }
                )
        rows.sort(key=lambda x: (-(x["total"] or 0), -x["number"]))
        return rows

    @r.post("/reviews")
    async def request_reviews(
        request: Request, body: ReviewsBody, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """Run a review or follow-up for each selected PR; drafts go to the requester's inbox."""
        service = service_of(request)
        out = []
        for target in body.prs[:50]:
            await require_view(service, user, target.repo)
            out.append(await _request_review(service, user, target.repo, target.number))
        return out

    @r.get("/prs/{owner}/{name}/{number}")
    async def pr_detail(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        pr = await service.store.get_snapshot(repo, number)
        if pr is None:
            raise HTTPException(404, f"{repo}#{number} is not known yet")
        profile = await service.profiles.active(repo)
        actions = await service.store.list_actions(repo=repo, number=number, limit=200)
        proposals = {
            p.action_id: p for p in await service.store.list_proposals(repo=repo)
        }
        arm = await service.store.get_arm(repo, number)
        gate = None
        if profile is not None and arm is not None:
            info = await service.a.forge.get_repo(repo)
            perms = {
                r_.author: await permission(service, User(login=r_.author), repo)
                for r_ in pr.reviews
            }
            gate = evaluate_merge_gate(
                pr,
                profile,
                info,
                arm,
                perms,
                service.info.identities,
                {},
                False,
                service.clock.now(),
            ).model_dump(mode="json")
        cls = classify_review_state(
            pr,
            service.info.identities
            | set(profile.people.review_identities if profile else []),
            profile.comments.legacy_markers if profile else [],
        )
        ev = await service.store.get_evaluation(repo, number)
        return {
            "pr": pr.model_dump(mode="json"),
            "review_state": cls.model_dump(mode="json"),
            "evaluation": ev.model_dump(mode="json") if ev else None,
            "actions": [
                {
                    **a.model_dump(mode="json"),
                    "proposal": proposals[a.action.id].model_dump(mode="json")
                    if a.action.id in proposals
                    else None,
                }
                for a in actions
            ],
            "reviews": [
                r_.model_dump(mode="json")
                for r_ in await service.store.list_review_records(repo, number)
            ],
            "ci": [
                o.model_dump(mode="json")
                for o in await service.store.list_ci_observations(repo)
                if o.number == number
            ],
            "hygiene": await service.store.get_state(hygiene_key(repo, number)),
            "arm": arm.model_dump(mode="json") if arm else None,
            "gate": gate,
        }

    @r.post("/prs/{owner}/{name}/{number}/override")
    async def override(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: OverrideBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if not body.reason.strip():
            raise HTTPException(422, "an override needs a reason")
        profile = await service.profiles.active(repo)
        pr = await service.store.get_snapshot(repo, number)
        if profile is None or pr is None:
            raise HTTPException(404, "unknown PR or repository without a profile")
        rubric = rubric_of(profile)
        before = await service.store.get_evaluation(repo, number) or Evaluation(
            repo=repo, number=number, title=pr.title
        )
        update: dict[str, Any] = {
            "override_reason": body.reason,
            "override_by": user.login,
            "override_at": service.clock.now(),
        }
        if body.scores is not None:
            unknown = sorted(set(body.scores) - set(rubric.dimensions))
            if unknown:
                raise HTTPException(422, f"unknown dimensions {unknown}")
            update["scores"] = {**before.scores, **body.scores}
        if body.manual_boost is not None:
            update["manual_boost"] = body.manual_boost
        if body.tier is not None:
            if body.tier not in rubric.tier_names() and body.tier != "":
                raise HTTPException(422, f"unknown tier {body.tier}")
            update["tier_override"] = body.tier or None
        goals = await service.knowledge.stage_goals(repo, profile)
        state = classify_review_state(
            pr, service.info.identities, profile.comments.legacy_markers
        ).state
        after = recompute(
            before.model_copy(update=update),
            rubric,
            goals,
            haystack(pr, before, state),
            service.clock.now(),
        )
        await service.store.save_evaluation(after)
        payload = {
            "number": number,
            "before": {
                "tier": before.tier,
                "total": before.total,
                "scores": before.scores,
            },
            "after": {
                "tier": after.tier,
                "total": after.total,
                "scores": after.scores,
                "manual_boost": after.manual_boost,
            },
            "reason": body.reason,
            "by": user.login,
        }
        await service.store.add_signal(
            Signal(
                kind=SignalKind.PRIORITY_OVERRIDE,
                repo=repo,
                number=number,
                source=user.login,
                payload=payload,
            )
        )
        await service.bus.publish(
            Event(
                kind=EventKind.PRIORITY_OVERRIDDEN,
                repo=repo,
                number=number,
                payload=payload,
                source="dashboard",
            )
        )
        return after.model_dump(mode="json")

    @r.post("/prs/{owner}/{name}/{number}/claim")
    async def claim(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.store.set_claim(repo, number, user.login)
        return {"claimed_by": user.login}

    @r.delete("/prs/{owner}/{name}/{number}/claim")
    async def unclaim(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.store.set_claim(repo, number, None)
        return {"claimed_by": None}

    @r.post("/prs/{owner}/{name}/{number}/merge")
    async def arm(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        pr = await service.a.actuator_reader.get_pr(repo, number)
        if profile is None or pr is None or not profile.merge.enabled:
            raise HTTPException(409, "merging is not enabled for this repository")
        perm = await service.a.forge.get_permission(repo, user.login)
        who = profile.merge.arming.who
        if who != "codeowners" and not perm.at_least(Permission(who)):
            raise HTTPException(403, f"arming a merge needs {who}")
        now = service.clock.now()
        arm_ = MergeArm(
            repo=repo,
            number=number,
            sha=pr.head_sha,
            armed_by=user.login,
            armed_at=now,
            armer_permission=perm,
        )
        await service.store.save_arm(arm_)
        await service.bus.publish(
            Event(
                kind=EventKind.MERGE_ARMED,
                repo=repo,
                number=number,
                sha=pr.head_sha,
                payload={"by": user.login, "armed_at": now.isoformat()},
            )
        )
        return arm_.model_dump(mode="json")

    @r.delete("/prs/{owner}/{name}/{number}/merge")
    async def disarm(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.store.delete_arm(repo, number)
        await service.bus.publish(
            Event(
                kind=EventKind.MERGE_DISARMED,
                repo=repo,
                number=number,
                payload={"by": user.login},
            )
        )
        return {"armed": False}

    @r.post("/prs/{owner}/{name}/{number}/review")
    async def request_review(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        return await _request_review(service, user, repo, number)

    @r.post("/prs/{owner}/{name}/{number}/lab")
    async def lab(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: LabBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            payload={
                "command": "lab_test",
                "lab": body.lab,
                "template": body.template,
                "args": body.args,
                "by": user.login,
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"event": event.id}

    # -- proposals -------------------------------------------------------------------------

    @r.get("/proposals")
    async def proposals(
        request: Request,
        state: ProposalState | None = ProposalState.PENDING,
        repo: str | None = None,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        out = []
        for p in await service.store.list_proposals(state, repo):
            if p.repo not in visible:
                continue
            record = await service.store.get_action(p.action_id)
            out.append(
                {
                    **p.model_dump(mode="json"),
                    "action": record.model_dump(mode="json") if record else None,
                }
            )
        return out

    @r.post("/proposals/{proposal_id}/approve")
    async def approve(
        request: Request,
        proposal_id: str,
        body: ApproveBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        proposal = await service.store.get_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(404, "unknown proposal")
        await require_view(service, user, proposal.repo)
        try:
            proposal = await service.pipeline.approve(
                proposal_id, user.login, body.payload
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return proposal.model_dump(mode="json")

    @r.post("/proposals/{proposal_id}/reject")
    async def reject(
        request: Request,
        proposal_id: str,
        body: RejectBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        proposal = await service.store.get_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(404, "unknown proposal")
        await require_view(service, user, proposal.repo)
        try:
            proposal = await service.pipeline.reject(
                proposal_id, user.login, body.reason
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return proposal.model_dump(mode="json")

    # -- agents' questions -------------------------------------------------------------

    @r.get("/questions")
    async def questions(
        request: Request,
        state: QuestionState | None = QuestionState.PENDING,
        repo: str | None = None,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        if repo:
            await require_view(service, user, repo)
        return [
            q.model_dump(mode="json")
            for q in await service.store.list_questions(state, repo)
            if await may_answer(service, user, q)
        ]

    @r.post("/questions/{question_id}/answer")
    async def answer_question(
        request: Request,
        question_id: str,
        body: AnswerBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        question = await service.store.get_question(question_id)
        if question is None:
            raise HTTPException(404, "unknown question")
        if not await may_answer(service, user, question):
            raise HTTPException(
                403,
                f"only maintainers of {question.repo} can answer this question"
                if question.repo
                else "only operators can answer questions outside a repository",
            )
        try:
            updated = await service.questions.answer(
                question_id,
                AgentAnswer(
                    allow=body.allow,
                    option_id=body.option_id,
                    answers=body.answers,
                    message=body.message,
                ),
                user.login,
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        await service.store.add_audit(
            AuditEntry(
                kind="agent_question",
                repo=question.repo,
                number=question.pr,
                actor=user.login,
                summary=f"{'allowed' if updated.answer and updated.answer.allow else 'denied'}"
                f" {question.runtime}: {question.title}",
            )
        )
        return updated.model_dump(mode="json")

    # -- memory ----------------------------------------------------------------------------

    @r.get("/memory/changes")
    async def memory_changes(
        request: Request,
        state: MemoryChangeState | None = MemoryChangeState.OPEN,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        return [
            c.model_dump(mode="json")
            for c in await service.store.list_memory_changes(state)
            if set(c.repos) <= visible and c.repos
        ]

    @r.post("/memory/changes/{change_id}/approve")
    async def approve_memory(
        request: Request, change_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        try:
            decisions, state = await service.approve_memory_change(
                change_id, user.login
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if not any(d.allowed for d in decisions):
            raise HTTPException(403, "; ".join(d.reason for d in decisions))
        return {
            "state": state,
            "decisions": [d.model_dump(mode="json") for d in decisions],
        }

    @r.post("/memory/changes/{change_id}/reject")
    async def reject_memory(
        request: Request,
        change_id: str,
        body: RejectBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        try:
            change = await service.reject_memory_change(
                change_id, user.login, body.reason
            )
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return change.model_dump(mode="json")

    @r.get("/memory/tree")
    async def memory_tree(
        request: Request, prefix: str = "", user: User = Depends(current_user)
    ) -> list[str]:
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        files = await service.a.memory.list_files(prefix)
        return [
            f
            for f in files
            if (repos_of_path(f) and repos_of_path(f)[0] in visible)
            or f.startswith("global/")
            or f == "MEMORY.md"
        ]

    @r.get("/memory/file")
    async def memory_file(
        request: Request, path: str = Query(...), user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        owners = repos_of_path(path)
        if owners:
            await require_view(service, user, owners[0])
        elif not (
            path.startswith("global/")
            or path == "MEMORY.md"
            or user.login in service.settings.operators
        ):
            raise HTTPException(403, "only operators can read operator settings")
        text = await service.a.memory.read_text(path)
        if text is None:
            raise HTTPException(404, "no such file")
        return {"path": path, "text": text}

    # -- operations ----------------------------------------------------------------------

    @r.get("/ci")
    async def ci(
        request: Request, repo: str | None = None, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repos_ = [repo] if repo else await visible_repos(service, user)
        if repo:
            await require_view(service, user, repo)
        since = service.clock.now() - timedelta(days=14)
        observations = [
            o
            for name in repos_
            for o in await service.store.list_ci_observations(name, since=since)
        ]
        by_job: dict[str, dict[str, int]] = {}
        for o in observations:
            row = by_job.setdefault(
                f"{o.repo}:{o.workflow}/{o.check_name}",
                {"flaky": 0, "infra": 0, "pr_caused": 0, "unknown": 0},
            )
            row[o.category] = row.get(o.category, 0) + 1
        return {
            "observations": [o.model_dump(mode="json") for o in observations[-200:]],
            "by_job": by_job,
        }

    @r.get("/agents")
    async def agents(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        runs = await service.store.list_agent_runs(200)
        visible = set(await visible_repos(service, user))
        return {
            "health": [
                h.model_dump(mode="json") for h in await service.a.agents.health()
            ],
            "runs": [
                r_.model_dump(mode="json")
                for r_ in runs
                if r_.repo is None or r_.repo in visible
            ],
        }

    @r.get("/jobs")
    async def jobs(
        request: Request,
        status: JobStatus | None = None,
        user: User = Depends(current_user),
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        return [
            j.model_dump(mode="json")
            for j in await service.store.list_jobs([status] if status else None)
            if j.repo is None or j.repo in visible
        ]

    @r.get("/audit")
    async def audit(
        request: Request, repo: str | None = None, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        visible = set(await visible_repos(service, user))
        if repo:
            await require_view(service, user, repo)
        return [
            a.model_dump(mode="json")
            for a in await service.store.list_audit(repo)
            if a.repo is None or a.repo in visible
        ]

    @r.post("/pause")
    async def pause(
        request: Request, body: PauseBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        if user.login not in service.settings.operators and not await visible_repos(
            service, user
        ):
            raise HTTPException(
                403, "only operators and maintainers can pause the service"
            )
        await service.pipeline.set_paused(True, user.login, None, body.reason)
        return {"paused": True}

    @r.post("/resume")
    async def resume(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        if user.login not in service.settings.operators:
            raise HTTPException(403, "only operators can resume the whole service")
        await service.pipeline.set_paused(False, user.login, None)
        return {"paused": False}

    @r.get("/events")
    async def events(
        request: Request, user: User = Depends(current_user)
    ) -> StreamingResponse:
        service = service_of(request)
        visible = set(await visible_repos(service, user))

        async def stream() -> Any:
            yield "retry: 5000\n\n"
            async for event in service.bus.stream():
                if await request.is_disconnected():
                    break
                if event.repo is not None and event.repo not in visible:
                    continue
                data = {
                    "kind": event.kind,
                    "repo": event.repo,
                    "number": event.number,
                    "id": event.id,
                }
                if event.kind == EventKind.QUESTION_ASKED and (
                    event.repo is not None or user.login in service.settings.operators
                ):
                    data["title"] = event.payload.get("title")
                yield f"event: {event.kind}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return r


async def agreement_scores(service: Service, repo: str) -> dict[str, Any]:
    """For shadow labels and merges: whether a maintainer later did the same thing (design 5.8)."""
    shadow = await service.store.list_actions(
        repo=repo, statuses=[ActionStatus.SHADOWED], limit=2000
    )
    scores: dict[str, dict[str, int]] = {}
    for record in shadow:
        a = record.action
        row = scores.setdefault(str(a.kind), {"matched": 0, "total": 0, "unknown": 0})
        pr = await service.store.get_snapshot(repo, a.number) if a.number else None
        if pr is None:
            row["unknown"] += 1
            continue
        if a.kind in (ActionKind.LABEL_APPLY, ActionKind.LABEL_CI_GATE):
            profile: RepoProfile | None = await service.profiles.active(repo)
            wanted = [
                *(a.payload.get("labels") or []),
                *[
                    profile.label_for(r)
                    for r in a.payload.get("roles", [])
                    if profile and profile.label_for(r)
                ],
            ]
            row["total"] += 1
            row["matched"] += int(bool(wanted) and all(w in pr.labels for w in wanted))
        elif a.kind == ActionKind.PR_MERGE:
            row["total"] += 1
            row["matched"] += int(pr.state.value == "MERGED")
        else:
            row["unknown"] += 1
    return {
        "shadowed": len(shadow),
        "kinds": {
            kind: {
                **row,
                "agreement": round(row["matched"] / row["total"], 2)
                if row["total"]
                else None,
            }
            for kind, row in scores.items()
        },
    }


async def _request_review(
    service: Service, user: User, repo: str, number: int
) -> dict[str, Any]:
    """Publish a review request for one PR and say what it will run."""
    pr = await service.store.get_snapshot(repo, number)
    profile = await service.profiles.active(repo)
    if pr is None or profile is None:
        return {
            "repo": repo,
            "number": number,
            "run": "skip",
            "reason": "unknown PR or repository without a profile",
        }
    ids = service.info.identities | set(profile.people.review_identities)
    state = classify_review_state(
        pr, ids, profile.comments.legacy_markers, include_drafts=True
    ).state
    if state == "UP_TO_DATE":
        return {
            "repo": repo,
            "number": number,
            "run": "skip",
            "reason": "already reviewed at the current head",
        }
    run = (
        "followup" if state in ("FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS") else "review"
    )
    event = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=repo,
        number=number,
        sha=pr.head_sha,
        payload={"command": "review", "by": user.login, "source": "dashboard"},
        source="dashboard",
    )
    await service.bus.publish(event)
    return {
        "repo": repo,
        "number": number,
        "run": run,
        "event": event.id,
        "state": state,
    }


async def review_activity(service: Service, repo: str) -> dict[int, dict[str, Any]]:
    """Per PR: the latest review or follow-up job, a pending draft, and the last posted review."""
    out: dict[int, dict[str, Any]] = {}
    for job in await service.store.list_jobs(repo=repo, limit=500):
        if job.playbook not in ("review", "followup") or job.number is None:
            continue
        row = out.setdefault(job.number, {})
        if "job" not in row:
            row["job"] = {
                "playbook": job.playbook,
                "status": job.status,
                "updated_at": job.updated_at.isoformat(),
                "result": job.result,
                "error": job.error,
            }
    for proposal in await service.store.list_proposals(ProposalState.PENDING, repo):
        record = await service.store.get_action(proposal.action_id)
        if (
            record is None
            or record.action.kind != ActionKind.REVIEW_POST
            or record.action.number is None
        ):
            continue
        out.setdefault(record.action.number, {}).setdefault(
            "draft",
            {
                "proposal": proposal.id,
                "for": proposal.requested_by,
                "sha": (record.action.head_sha or "")[:7],
            },
        )
    for record in await service.store.list_actions(
        repo=repo,
        kinds=[ActionKind.REVIEW_POST],
        statuses=[ActionStatus.EXECUTED],
        limit=500,
    ):
        if record.action.number is not None:
            out.setdefault(record.action.number, {}).setdefault(
                "posted",
                {"sha": (record.action.head_sha or "")[:7], "by": record.approver},
            )
    return out
