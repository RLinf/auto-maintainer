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
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
    ApprovePrPayload,
    ApproveRunsPayload,
    Autonomy,
    CodeMergePayload,
    LabelPayload,
    LabReproducePayload,
    MemoryChangePayload,
    Proposal,
    ProposalState,
    Provenance,
    RequestReviewersPayload,
    RerunPayload,
    ServiceDeployPayload,
    UpdateBranchPayload,
    make_action,
)
from ..domain.agents import AgentAnswer, AgentQuestion, QuestionState, SkillInfo
from ..domain.events import Event, EventKind
from ..domain.ids import check_login, check_repo, new_id, same_commit
from ..domain.labs import FIX_CI_SESSION, time_limit, unusable
from ..domain.memory import (
    MemoryChange,
    MemoryChangeState,
    Signal,
    SignalKind,
    lock_path,
    profile_path,
    repo_dir,
    repos_of_path,
)
from ..domain.merge_gate import MergeArm, evaluate_merge_gate
from ..domain.permissions import Permission
from ..domain.profile import Conditions, RepoProfile, Review
from ..domain.records import (
    ActionRecord,
    AgentRunRecord,
    AuditEntry,
    Job,
    JobStatus,
    PrAssignee,
)
from ..domain.review import (
    ReviewQuality,
    classify_review_state,
    review_chain,
    review_quality,
)
from ..domain.run_ci import plan_run_ci
from ..domain.scoring import Evaluation, recompute
from ..domain.secrets import redact_secrets
from ..domain.snapshot import Mergeable, PrState, ReviewEventState
from ..playbooks.evaluate import haystack, rubric_of
from ..playbooks.hygiene import hygiene_key
from ..playbooks.prompts import DEFAULT_QUALITY_GUIDE, DEFAULT_WRITING_GUIDE
from ..playbooks.quality import (
    assessed_quality,
    quick_lessons,
    quick_misses,
    quick_quality,
)
from ..profiles.layers import ProfileError, parse_yaml
from ..profiles.lock import parse_lock, profile_diff
from ..profiles.skills import (
    check_skill_choice,
    repository_skills,
    review_skill_in_use,
    service_skills,
)
from ..service import Service
from .auth import User, current_user

PERMISSION_TTL = timedelta(minutes=10)

log = logging.getLogger(__name__)
T = TypeVar("T")


async def start_evaluations(
    service: Service, login: str, targets: list[tuple[str, int]], source: str
) -> list[dict[str, Any]]:
    """Score each PR's priority again on request, every dimension, with the
    repository's lessons as they are now: at most ``MAX_EVALUATIONS``, skipping
    repositories whose priority is off, and joining a re-score already running for
    a PR instead of starting another. The jobs share a ``batch`` id, which the
    dashboard counts their progress by. The caller checks the user may view each one."""
    batch = new_id("batch")
    manual: dict[tuple[str | None, int | None], Any] = {}
    for job in await service.store.list_jobs(
        [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING], limit=5000
    ):
        if job.playbook == "evaluate" and ":manual:" in job.key:
            manual[(job.repo, job.number)] = job
    out = []
    for repo, number in targets[:MAX_EVALUATIONS]:
        item: dict[str, Any] = {"repo": repo, "number": number}
        profile = await service.profiles.active(repo)
        if profile is None or not profile.priority.enabled:
            out.append({**item, "run": "skip", "reason": "priority is off here"})
            continue
        running = manual.get((repo, number))
        if running is not None:
            first = running.events[0].get("id") if running.events else None
            out.append({**item, "run": "joined", "event": first or running.id})
            continue
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            payload={"command": "evaluate", "by": login, "batch": batch},
            source=source,
        )
        await service.bus.publish(event)
        out.append({**item, "run": "evaluate", "event": event.id})
    return out


def _memory_write_failed(error: str) -> str:
    return (
        "The approval is recorded, but writing it to the memory repo failed: "
        f"{redact_secrets(error)[0]}. Approve again to retry."
    )


async def _profile_changes(
    service: Service, change: MemoryChange
) -> list[dict[str, Any]]:
    """For each repository whose lock ``change`` writes: the approved profile and the
    one the change would lock, so the dashboard can show what changes in words."""
    out = []
    for repo in change.repos:
        path = lock_path(repo)
        try:
            new = parse_lock(change.files.get(path), path)
            old = await service.profiles.lock(repo)
        except ProfileError:
            continue
        if new is not None:
            out.append(
                {
                    "repo": repo,
                    "before": old.profile if old else None,
                    "after": new.profile,
                }
            )
    return out


async def _merge_memory(merge: Awaitable[T]) -> T:
    """Await an approval that writes to the memory repo, turning a failed write into
    a 502 that says so; ``ValueError`` (the change is not open) passes through."""
    try:
        return await merge
    except (HTTPException, ValueError):
        raise
    except Exception as e:
        log.exception("writing a memory change failed")
        raise HTTPException(502, _memory_write_failed(str(e))) from e


SKILLS_TTL = 600.0
"""How long a repository's skills, read from its default branch, are kept (seconds)."""
_SKILLS: dict[str, tuple[float, list[SkillInfo]]] = {}


async def repo_skills_of(
    service: Service, repo: str, *, refresh: bool = False
) -> list[SkillInfo]:
    """The repository's own skills on its default branch, kept for ``SKILLS_TTL``."""
    now = time.monotonic()
    kept = _SKILLS.get(repo)
    if kept and not refresh and now - kept[0] < SKILLS_TTL:
        return kept[1]
    try:
        found = await repository_skills(service.a.forge, repo)
    except Exception:
        log.exception("reading %s's skills failed", repo)
        return kept[1] if kept else []
    _SKILLS[repo] = (now, found)
    return found


async def review_setup(
    service: Service, repo: str, profile: RepoProfile, *, refresh: bool = False
) -> dict[str, Any]:
    """How the repository's PRs are reviewed: the skill the profile names, what a
    review follows under it, the skills to choose from, the maintainers' own
    instructions, and whether nobody chose yet while the repository has no review
    skill of its own (``needs_choice``)."""
    own = await repo_skills_of(service, repo, refresh=refresh)
    ours = service_skills(Path(service.settings.skills_dir))
    kind, skill = review_skill_in_use(profile, own)
    default = Review.model_fields["skill"].default
    named = profile.review.skill.split(":", 1)[1]
    return {
        "skill": profile.review.skill,
        "default_skill": default,
        "instructions": profile.review.instructions,
        "in_use": {
            "kind": kind,
            "name": skill.name if skill else named,
            "path": skill.path if skill else "",
        },
        "repo_skills": [{**s.model_dump(), "reviews": s.reviews} for s in own],
        "service_skills": [s.model_dump() for s in ours],
        "needs_choice": profile.review.skill == default
        and kind == "service"
        and not (profile.review.instructions or "").strip(),
    }


MAX_DIFF = 2_000_000
_DIFFS: dict[tuple[str, int, str], str] = {}
"""Recent PR diffs by (repo, number, head SHA); a head never changes its diff."""


async def _run_now(
    service: Service, user: User, action: Action, *, as_them: bool = False
) -> ActionRecord:
    """Submit ``action`` as drafted for ``user`` and approve it as them at once: a
    button a person pressed. The policy decides again on approval, so a pause, a
    budget, or a hard limit can still hold or deny it; the record says which. With
    ``as_them``, it runs with their own GitHub account."""
    record = await service.pipeline.submit(action)
    if record.status == ActionStatus.PROPOSED:
        proposal = next(
            (
                p
                for p in await service.store.list_proposals(
                    ProposalState.PENDING, action.repo
                )
                if p.action_id == record.action.id
            ),
            None,
        )
        if proposal is not None:
            try:
                await service.pipeline.approve(
                    proposal.id, user.login, post_as=user.login if as_them else None
                )
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
    return await service.store.get_action(record.action.id) or record


USAGE_RUNS = 20_000
"""The most runs a usage summary reads; the answer says when it hit this."""


def _job_requester(job: Job) -> str | None:
    """Who asked for a job: ``by`` in the payload of the event that started it."""
    first = job.events[0] if job.events else {}
    payload = (first.get("payload") if isinstance(first, dict) else None) or {}
    by = payload.get("by")
    return str(by) if by else None


def _usage_row(login: str | None) -> dict[str, Any]:
    return {
        "login": login,
        "runs": 0,
        "failed": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "seconds": 0.0,
        "last_at": None,
        "by_kind": {},
        "by_runtime": {},
    }


def _add_usage(row: dict[str, Any], run: AgentRunRecord) -> None:
    """Count ``run`` into a usage row; a cost adds up only from runs that report one."""
    row["runs"] += 1
    row["failed"] += 0 if run.ok else 1
    row["input_tokens"] += run.input_tokens
    row["output_tokens"] += run.output_tokens
    row["seconds"] += run.duration_seconds
    if run.cost_usd is not None:
        row["cost_usd"] = (row["cost_usd"] or 0.0) + run.cost_usd
    at = run.created_at.isoformat()
    if row["last_at"] is None or at > row["last_at"]:
        row["last_at"] = at
    for key, value in (("by_kind", run.task_kind), ("by_runtime", run.runtime)):
        part = row[key].setdefault(value, {"runs": 0, "tokens": 0})
        part["runs"] += 1
        part["tokens"] += run.input_tokens + run.output_tokens


def assignee_row(a: PrAssignee) -> dict[str, Any]:
    return {"login": a.login, "kind": a.kind, "by": a.by, "at": a.at.isoformat()}


async def _assignees(service: Service, repo: str, number: int) -> list[dict[str, Any]]:
    return [assignee_row(a) for a in await service.store.list_assignees(repo, number)]


async def _merge_queue(service: Service, repo: str) -> dict[str, Any]:
    """The repository's armed PRs in the order they merge, as the merge playbook last
    worked it out, and whose turn it is (``merge-queue:<repo>``)."""
    state = await service.store.get_state(f"merge-queue:{repo}") or {}
    armed = {a.number: a for a in await service.store.list_arms(repo)}
    order = [n for n in state.get("order") or [] if n in armed]
    order += sorted(n for n in armed if n not in order)
    head = state.get("head") if state.get("head") in armed else None
    if head is not None:
        order = [head, *(n for n in order if n != head)]
    titles = {}
    for n in order:
        snap = await service.store.get_snapshot(repo, n)
        titles[n] = snap.title if snap else ""
    return {
        "head": head,
        "order": [{"number": n, "title": titles[n]} for n in order],
    }


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


async def may_view(service: Service, user: User, repo: str) -> bool:
    """Whether ``user`` may use the dashboard for ``repo`` (design 2.3): write access
    on GitHub, or a place in its People record with access to it."""
    perm = await permission(service, user, repo)
    if perm.at_least(Permission.WRITE):
        return True
    return perm.at_least(Permission.READ) and (
        user.login.lower() in await service.governance.members_of(repo)
    )


async def require_view(service: Service, user: User, repo: str) -> None:
    check_repo(repo)
    if not await may_view(service, user, repo):
        raise HTTPException(
            403,
            f"you need write access to {repo}, or a place in its People record, "
            "to see it here",
        )


async def visible_repos(service: Service, user: User) -> list[str]:
    out = []
    for record in await service.store.list_repos():
        if await may_view(service, user, record.repo):
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
    as_me: bool = False
    """Post this review draft with the approver's own GitHub account."""


class RejectBody(BaseModel):
    reason: str


class EditBody(BaseModel):
    payload: dict[str, Any]


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


class ApprovePrBody(BaseModel):
    body: str = Field(default="", max_length=20_000)
    head: str | None = None
    """The head the person looked at; a newer one refuses the approval."""


class AssignBody(BaseModel):
    logins: list[str] = Field(min_length=1, max_length=10)
    request_review: bool = True
    """Also request their review on GitHub, under the repository's rule."""


class AnswerBody(BaseModel):
    allow: bool = True
    option_id: str | None = None
    answers: dict[str, list[str]] = Field(default_factory=dict)
    message: str = Field(default="", max_length=4000)


async def is_maintainer(service: Service, user: User, repo: str) -> bool:
    """Whether ``user`` maintains ``repo`` (``ConfigGate``: its People record's
    maintainers, else its GitHub admins), checked live."""
    profile = await service.profiles.active(repo)
    decision = await service.gate.check(repo, user.login, profile, person=True)
    return decision.allowed


def proposal_requester(proposal: Proposal, record: ActionRecord | None) -> str | None:
    """Who asked for a proposal's action: the reviewer a draft is for, or the person
    whose request on the dashboard or in chat produced it; None for the service's
    own work."""
    if proposal.requested_by:
        return proposal.requested_by
    return record.action.provenance.requested_by if record else None


async def may_decide(
    service: Service,
    user: User,
    proposal: Proposal,
    record: ActionRecord | None,
    *,
    maintainer: bool | None = None,
) -> bool:
    """Whether ``user`` approves, edits, or rejects ``proposal`` (design 2.3): its
    repository's maintainers decide every proposal, and a reviewer decides the ones
    they asked for. ``maintainer`` passes a status already checked."""
    requester = proposal_requester(proposal, record)
    if requester and requester.lower() == user.login.lower():
        return True
    if maintainer is None:
        maintainer = await is_maintainer(service, user, proposal.repo)
    return maintainer


async def require_decide(service: Service, user: User, proposal: Proposal) -> None:
    await require_view(service, user, proposal.repo)
    record = await service.store.get_action(proposal.action_id)
    if not await may_decide(service, user, proposal, record):
        raise HTTPException(
            403,
            f"a maintainer of {proposal.repo} decides this; you did not ask for it",
        )


async def may_answer(service: Service, user: User, question: AgentQuestion) -> bool:
    """Who answers an agent's question: the person who asked for its work, if they
    may see the repository, or one of its maintainers; operators answer for work
    outside a repository."""
    if question.repo is None:
        return user.login in service.settings.operators
    if not await may_view(service, user, question.repo):
        return False
    if question.asked_for and question.asked_for.lower() == user.login.lower():
        return True
    return await is_maintainer(service, user, question.repo)


class DeployBody(BaseModel):
    sha: str = Field(min_length=7, max_length=40, pattern=r"^[0-9a-f]+$")


async def code_maintainer(service: Service, user: User) -> tuple[bool, str]:
    """Whether ``user`` may merge and deploy the service's own code, and why not."""
    repo = service.settings.code_repo
    if not repo:
        return False, "no code repo is configured"
    need = Permission(service.settings.code_approver)
    decision = await service.gate.check_service(repo, user.login, need, person=True)
    if decision.allowed:
        return True, ""
    return (
        False,
        f"only users with {need.value} on {repo} can approve changes to the service",
    )


class PrTarget(BaseModel):
    repo: str
    number: int


class LevelBody(BaseModel):
    level: Autonomy
    conditions: Conditions | None = None
    otherwise: Autonomy | None = None


class AutomationBody(BaseModel):
    trigger: Literal["manual", "scan", "events"]
    every_hours: int = Field(default=6, ge=1, le=168)
    max_prs: int = Field(default=5, ge=1, le=50)
    followups: bool = True


class ReviewGuidesBody(BaseModel):
    writing_guide: str | None = Field(default=None, max_length=8000)
    quality_guide: str | None = Field(default=None, max_length=8000)
    """Empty, or the service's default, restores the default."""
    quick_quality: bool = True
    skill: str | None = Field(default=None, max_length=300)
    """The review skill (``review.skill``); None leaves it as it is."""
    instructions: str | None = Field(default=None, max_length=8000)
    """The maintainers' review instructions; None leaves them, empty removes them."""


def cron_every(hours: int) -> str:
    """A cron schedule (UTC) for every ``hours`` hours; daily and weekly at 02:00."""
    if hours >= 168:
        return "0 2 * * 1"
    if hours >= 24:
        return "0 2 * * *"
    return f"0 */{hours} * * *" if hours > 1 else "0 * * * *"


def hours_of(cron: str) -> int | None:
    """The interval ``cron_every`` would turn into ``cron``, if it is one of those."""
    if cron == "0 2 * * 1":
        return 168
    if cron == "0 2 * * *":
        return 24
    if cron == "0 * * * *":
        return 1
    m = re.fullmatch(r"0 \*/(\d+) \* \* \*", cron)
    return int(m.group(1)) if m else None


class ReviewsBody(BaseModel):
    prs: list[PrTarget]
    agent: str | None = None
    """The runtime to review with; empty follows each repository's route."""


class ReviewBody(BaseModel):
    agent: str | None = None


MAX_EVALUATIONS = 500
"""PRs one request may re-score; each runs an agent."""


class EvaluationsBody(BaseModel):
    prs: list[PrTarget]


class QualityBody(EvaluationsBody):
    quick: bool = False
    """Score quickly from the diff even a PR that has a review."""


class RefreshBody(BaseModel):
    repos: list[str] = Field(default_factory=list)
    """Repositories to read now; empty means every one the user sees."""


class MergeBaseBody(BaseModel):
    agent: str | None = None
    """The agent that resolves conflicts, when there are any."""


class FixCiBody(BaseModel):
    agent: str | None = None
    lab: str | None = None
    """A test machine to test the fix on; none tests nothing."""
    gpu: bool = False
    """Whether the tests need a GPU: the session leases an idle one."""
    note: str = Field(default="", max_length=2000)


class FixBody(BaseModel):
    review: str | None = None
    """The review action whose findings to fix; the PR's latest review by default."""
    findings: list[str] = Field(default_factory=list)
    """Finding ids to fix; all of them when empty."""
    agent: str | None = None
    note: str = Field(default="", max_length=2000)


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
            "can_post_as_me": await service.can_post_as(user.login),
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
            repo, user.login, await service.profiles.active(repo), person=True
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
        can = await service.gate.check(repo, user.login, current, person=True)
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
        pending = await service.open_lock_change(repo, body.hash)
        if pending is not None:
            # A retry after a failed write: approve the change already staged.
            change = pending
            _, status = await _merge_memory(
                service.approve_memory_change(change.id, user.login)
            )
        else:
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
            done = await service.store.get_action(record.action.id) or record
            if done.status == ActionStatus.FAILED:
                raise HTTPException(
                    502, _memory_write_failed(done.error or "unknown error")
                )
            status = str(done.status)
        stored = await service.store.get_memory_change(change.id)
        if stored and stored.state == MemoryChangeState.CONFLICT:
            raise HTTPException(
                409,
                "The approval is recorded, but the change conflicts with the memory "
                f"repo: {redact_secrets(stored.rejected_reason or '')[0]}",
            )
        return {
            "change": stored.model_dump(mode="json") if stored else None,
            "action": status,
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
        decision = await service.gate.check(
            repo, user.login, current, merged.profile, person=True
        )
        if decision.allowed:
            change.approvals = {repo: [user.login]}
        await service.store.save_memory_change(change)
        record = await service.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                repo,
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            )
        )
        await service.actuator.drain()
        done = await service.store.get_action(record.action.id) or record
        if done.status == ActionStatus.FAILED:
            raise HTTPException(
                502, _memory_write_failed(done.error or "unknown error")
            )
        stored = await service.store.get_memory_change(change.id)
        before = current.dump() if current else None
        return {
            "change": stored.model_dump(mode="json") if stored else None,
            "approved": decision.allowed,
            "gate": decision.model_dump(mode="json"),
            "diff": profile_diff(before, merged.data),
            "before": before,
            "after": merged.data,
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

    @r.get("/repos/{owner}/{name}/review-automation")
    async def review_automation(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """When reviews start on their own: only on request, periodically, or on every
        update; and whether updated PRs are re-reviewed."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        gate = await service.gate.check(repo, user.login, profile, person=True)
        review = profile.review
        return {
            "repo": repo,
            "trigger": review.trigger,
            "schedule": review.scan.schedule,
            "every_hours": hours_of(review.scan.schedule),
            "max_prs": review.scan.max_prs,
            "followups": review.scan.followups,
            "can_configure": gate.allowed,
            "reason": gate.reason,
        }

    @r.put("/repos/{owner}/{name}/review-automation")
    async def set_review_automation(
        request: Request,
        owner: str,
        name: str,
        body: AutomationBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Change it through the repository's settings layer; a maintainer applies it
        by making it, anyone else proposes it."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        current = await service.profiles.active(repo)
        layer = parse_yaml(
            await service.a.memory.read_text(profile_path(repo)) or "", "settings"
        )
        review = dict(layer.get("review") or {})
        review["trigger"] = body.trigger
        review["scan"] = {
            **dict(review.get("scan") or {}),
            "schedule": cron_every(body.every_hours),
            "max_prs": body.max_prs,
            "followups": body.followups,
        }
        layer["review"] = review
        what = {
            "manual": "review only on request",
            "scan": f"review every {body.every_hours}h",
            "events": "review every update",
        }[body.trigger]
        try:
            merged, change = await service.profiles.propose_memory_layer(
                repo, layer, user.login, f"review: {what}"
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

    @r.get("/repos/{owner}/{name}/review-guides")
    async def review_guides(
        request: Request,
        owner: str,
        name: str,
        refresh: bool = False,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """How the repository's PRs are reviewed (``review_setup``: the skill and the
        maintainers' instructions), how reviews are written, and how their agent
        judges a PR's quality: each guide as the repository sets it (``None`` for
        the default), and the defaults. ``refresh`` reads the repository's skills
        from GitHub again."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        gate = await service.gate.check(repo, user.login, profile, person=True)
        return {
            "repo": repo,
            **await review_setup(service, repo, profile, refresh=refresh),
            "writing_guide": profile.review.writing_guide,
            "quality_guide": profile.review.quality_guide,
            "quick_quality": profile.review.quick_quality,
            "quick_lessons": await quick_lessons(service.store, repo),
            "quick_misses": [
                m.model_dump() for m in await quick_misses(service.store, repo)
            ][-10:],
            "default_writing_guide": DEFAULT_WRITING_GUIDE,
            "default_quality_guide": DEFAULT_QUALITY_GUIDE,
            "can_configure": gate.allowed,
            "reason": gate.reason,
        }

    @r.put("/repos/{owner}/{name}/review-guides")
    async def set_review_guides(
        request: Request,
        owner: str,
        name: str,
        body: ReviewGuidesBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Change the guides through the repository's settings layer; a maintainer
        applies it by making it, anyone else proposes it."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        current = await service.profiles.active(repo)
        layer = parse_yaml(
            await service.a.memory.read_text(profile_path(repo)) or "", "settings"
        )
        review = dict(layer.get("review") or {})
        if body.skill is not None:
            choice = body.skill.strip()
            problem = check_skill_choice(
                choice,
                await repo_skills_of(service, repo, refresh=True),
                service_skills(Path(service.settings.skills_dir)),
            )
            if problem:
                raise HTTPException(422, problem)
            if choice == Review.model_fields["skill"].default:
                review.pop("skill", None)
            else:
                review["skill"] = choice
        if body.instructions is not None:
            told = body.instructions.strip()
            if told:
                review["instructions"] = told + "\n"
            else:
                review.pop("instructions", None)
        for key, text, default in (
            ("writing_guide", body.writing_guide, DEFAULT_WRITING_GUIDE),
            ("quality_guide", body.quality_guide, DEFAULT_QUALITY_GUIDE),
        ):
            text = (text or "").strip()
            if text and text != default.strip():
                review[key] = text + "\n"
            else:
                review.pop(key, None)
        if body.quick_quality:
            review.pop("quick_quality", None)
        else:
            review["quick_quality"] = False
        if review:
            layer["review"] = review
        else:
            layer.pop("review", None)
        try:
            merged, change = await service.profiles.propose_memory_layer(
                repo,
                layer,
                user.login,
                "review: how PRs are reviewed, written, and scored",
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
            repo, user.login, await service.profiles.active(repo), person=True
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
        out = await agreement_scores(service, repo)
        profile = await service.profiles.active(repo)
        if profile is not None:
            gate = await service.gate.check(repo, user.login, profile, person=True)
            levels = {}
            for kind in ActionKind:
                if kind.touches_repository:
                    rule = profile.policy.get(kind)
                    levels[str(kind)] = str(rule.level if rule else Autonomy.SHADOW)
            out["levels"] = levels
            out["can_configure"] = gate.allowed
        return out

    @r.put("/repos/{owner}/{name}/policy/{kind}")
    async def set_level(
        request: Request,
        owner: str,
        name: str,
        kind: str,
        body: LevelBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """How far one action kind may go on its own in the repository (off, record
        only, propose, auto), in its settings layer: a maintainer applies it by making
        it, anyone else proposes it. Conditions on the level are kept unless the caller
        supplies them."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        try:
            action_kind = ActionKind(kind)
        except ValueError as e:
            raise HTTPException(404, f"there is no action kind {kind!r}") from e
        if not action_kind.touches_repository:
            raise HTTPException(422, f"{kind} does not act on the repository")
        current = await service.profiles.active(repo)
        if current is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        layer = parse_yaml(
            await service.a.memory.read_text(profile_path(repo)) or "", "settings"
        )
        policy = dict(layer.get("policy") or {})
        rule = dict(policy.get(kind) or {})
        rule["level"] = str(body.level)
        if body.conditions is not None:
            cond = body.conditions.model_dump(mode="json", exclude_defaults=True)
            if cond:
                rule["conditions"] = cond
            else:
                rule.pop("conditions", None)
        if body.otherwise is not None:
            if body.otherwise != Autonomy.SHADOW:
                rule["otherwise"] = str(body.otherwise)
            else:
                rule.pop("otherwise", None)
        policy[kind] = rule
        layer["policy"] = policy
        try:
            merged, change = await service.profiles.propose_memory_layer(
                repo, layer, user.login, f"policy: {kind} at {body.level}"
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
        on_pr: dict[tuple[str, int], list[PrAssignee]] = {}
        for a in await service.store.list_assignees(repo):
            on_pr.setdefault((a.repo, a.number), []).append(a)
        me = user.login.lower()
        rows = []
        for name in repos_:
            profile = await service.profiles.active(name)
            if profile is None:
                continue
            ids = service.info.identities | set(profile.people.review_identities)
            activity = await review_activity(service, name)
            quality = await review_qualities(service, name)
            own = await service.posted_as_people(name)
            for pr in await service.store.list_snapshots(name):
                cls = classify_review_state(
                    pr,
                    ids,
                    profile.comments.legacy_markers,
                    profile.scope.include_drafts,
                    own_actions=own,
                )
                ev = await service.store.get_evaluation(name, pr.number)
                assignees = on_pr.get((name, pr.number), [])
                if tier and (ev is None or ev.tier != tier):
                    continue
                if state and cls.state != state:
                    continue
                if (
                    mine
                    and all(a.login.lower() != me for a in assignees)
                    and me not in {r.lower() for r in pr.requested_reviewers}
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
                        "assignees": [assignee_row(a) for a in assignees],
                        "tier": ev.tier if ev else None,
                        "total": ev.total if ev else None,
                        "summary": ev.summary if ev else "",
                        "scores": ev.scores if ev else {},
                        "review_trigger": profile.review.trigger,
                        "review_activity": activity.get(pr.number, {}),
                        "quality": quality[pr.number].model_dump(mode="json")
                        if pr.number in quality
                        else None,
                        "quick_quality": quick.model_dump(mode="json")
                        if (
                            quick := await quick_quality(service.store, name, pr.number)
                        )
                        else None,
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
            out.append(
                await _request_review(
                    service, user, target.repo, target.number, body.agent
                )
            )
        return out

    @r.post("/prs/refresh")
    async def refresh_prs(
        request: Request, body: RefreshBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Read the PRs of ``body.repos`` (every repository the user sees when empty)
        from GitHub now, in the background, instead of at the next poll."""
        service = service_of(request)
        visible = await visible_repos(service, user)
        repos = [r_ for r_ in body.repos if r_ in visible] if body.repos else visible
        active = {rec.repo for rec in await service.store.list_repos() if rec.active}
        started, running = [], []
        for repo in repos:
            if repo not in active:
                continue
            (started if service.poll_now(repo) else running).append(repo)
        return {"started": started, "running": running}

    @r.post("/prs/{owner}/{name}/{number}/refresh")
    async def refresh_pr(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Read one PR from GitHub now and publish what changed."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        record = await service.store.get_repo(repo)
        if record is None or not record.active or not service.serves(repo):
            raise HTTPException(
                409, f"the service does not read {repo} until its profile is approved"
            )
        events = await service.poller.poll_pr(repo, number)
        return {"changes": [str(e.kind) for e in events]}

    @r.post("/evaluations")
    async def request_evaluations(
        request: Request, body: EvaluationsBody, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """Score each PR's priority again, every dimension, with the repository's
        lessons as they are now. A PR already being re-scored on request joins that
        job instead of starting another."""
        service = service_of(request)
        targets = body.prs[:MAX_EVALUATIONS]
        for target in targets:
            await require_view(service, user, target.repo)
        return await start_evaluations(
            service, user.login, [(t.repo, t.number) for t in targets], "dashboard"
        )

    @r.post("/quality")
    async def request_quality(
        request: Request, body: QualityBody, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """Have an agent score each PR's quality with the repository's quality guide
        as it is now: a reviewed PR from its latest review, any other (or every one,
        with ``quick``) quickly from its diff. A PR being scored already joins that
        job instead of starting another."""
        service = service_of(request)
        targets = body.prs[:MAX_EVALUATIONS]
        for target in targets:
            await require_view(service, user, target.repo)
        running = {
            (job.repo, job.number): job
            for job in await service.store.list_jobs(
                [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING], limit=5000
            )
            if job.playbook in ("quality", "quick_quality")
        }
        # One PR is scored at once in the agent pool, for the person waiting on it;
        # many go to the background pool when they need only a quick score, so a
        # whole view never holds up the reviews in the agent pool.
        many = len(targets) > 1
        out = []
        for target in targets:
            job = running.get((target.repo, target.number))
            if job is not None:
                first = job.events[0].get("id") if job.events else None
                out.append(
                    {
                        "repo": target.repo,
                        "number": target.number,
                        "run": "joined",
                        "event": first or job.id,
                    }
                )
                continue
            chain = review_chain(
                await service.store.list_actions(
                    repo=target.repo,
                    number=target.number,
                    kinds=[ActionKind.REVIEW_POST],
                    limit=500,
                )
            )
            snapshot = await service.store.get_snapshot(target.repo, target.number)
            moved = bool(chain) and (
                snapshot is not None
                and not same_commit(chain[-1].action.head_sha, snapshot.head_sha)
            )
            # No review, or new commits since it: the new head gets a quick score.
            quickly = body.quick or not chain or moved
            event = Event(
                kind=EventKind.QUICK_QUALITY
                if many and quickly
                else EventKind.QUALITY_REQUESTED,
                repo=target.repo,
                number=target.number,
                payload={"by": user.login, "quick": quickly},
                source="dashboard",
            )
            await service.bus.publish(event)
            out.append(
                {
                    "repo": target.repo,
                    "number": target.number,
                    "run": "score",
                    "event": event.id,
                }
            )
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
            own_actions=await service.posted_as_people(repo),
        )
        ev = await service.store.get_evaluation(repo, number)
        quality = review_quality(
            await service.store.list_actions(
                repo=repo, number=number, kinds=[ActionKind.REVIEW_POST], limit=500
            ),
            await assessed_quality(service.store, repo, number),
        )
        return {
            "pr": pr.model_dump(mode="json"),
            "assignees": await _assignees(service, repo, number),
            "review_state": cls.model_dump(mode="json"),
            "evaluation": ev.model_dump(mode="json") if ev else None,
            "quality": quality.model_dump(mode="json") if quality else None,
            "quick_quality": quick.model_dump(mode="json")
            if (quick := await quick_quality(service.store, repo, number))
            else None,
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
            "merge_queue": await _merge_queue(service, repo) if arm else None,
            "gate": gate,
            "run_ci": plan_run_ci(
                pr, profile.label_for("ci_gate") if profile else None
            ).model_dump(mode="json"),
        }

    @r.post("/prs/{owner}/{name}/{number}/approve")
    async def approve_pr(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: ApprovePrBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Approve the PR on GitHub as the person who pressed the button, with their
        own sign-in; the press is the approval, under the repository's
        ``pr.approve`` rule. The service's account never approves (hard limit), and
        whether the approval counts is GitHub's rule for that person."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if not await service.can_post_as(user.login):
            raise HTTPException(
                403, "sign in with GitHub, so the approval comes from you"
            )
        pr = await service.a.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(409, f"{repo}#{number} is not open")
        if pr.author.lower() == user.login.lower():
            raise HTTPException(409, "GitHub does not let you approve your own PR")
        if body.head and not same_commit(pr.head_sha, body.head):
            raise HTTPException(
                409,
                f"the PR moved to {pr.head_sha[:7]} since you looked; read the new "
                "commits, then approve",
            )
        # A comment after an approval leaves the approval standing, as on GitHub.
        mine = [
            r_
            for r_ in pr.reviews
            if r_.author.lower() == user.login.lower()
            and r_.state != ReviewEventState.COMMENTED
        ]
        if (
            mine
            and mine[-1].state == ReviewEventState.APPROVED
            and same_commit(mine[-1].commit_sha, pr.head_sha)
        ):
            raise HTTPException(409, "you already approved this head")
        record = await _run_now(
            service,
            user,
            make_action(
                ActionKind.PR_APPROVE,
                repo,
                ApprovePrPayload(body=body.body),
                number=number,
                head_sha=pr.head_sha,
                discriminator=f"approve:{user.login.lower()}:{new_id('req')}",
                rationale=f"{user.login} pressed Approve",
                draft_for=user.login,
                provenance=Provenance(requested_by=user.login),
            ),
            as_them=True,
        )
        return {
            "status": record.status,
            "error": record.error,
            "reason": record.decision.reason if record.decision else None,
            "rule": record.decision.rule if record.decision else None,
            "action": record.action.id,
        }

    @r.post("/prs/{owner}/{name}/{number}/merge-base")
    async def merge_base(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: MergeBaseBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Bring the PR's base branch into it, as the person who pressed the button.
        Without conflicts GitHub updates the branch at once (the press is the
        approval); with conflicts an agent resolves them and the merge comes back as
        a draft for them to read and push."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if not await service.can_post_as(user.login):
            raise HTTPException(
                403, "sign in with GitHub, so the merge is pushed as you"
            )
        pr = await service.a.forge.get_pr(
            repo, number
        ) or await service.store.get_snapshot(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(409, f"{repo}#{number} is not open")
        if pr.mergeable != Mergeable.CONFLICTING:
            record = await _run_now(
                service,
                user,
                make_action(
                    ActionKind.PR_UPDATE_BRANCH,
                    repo,
                    UpdateBranchPayload(),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator=f"merge-base:{new_id('req')}",
                    rationale=f"{user.login} pressed Merge {pr.base_ref}",
                    draft_for=user.login,
                ),
                as_them=True,
            )
            return {
                "run": "update",
                "status": record.status,
                "error": record.error,
                "action": record.action.id,
            }
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            payload={"command": "merge_base", "by": user.login, "agent": body.agent},
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"run": "resolve", "event": event.id}

    @r.post("/prs/{owner}/{name}/{number}/ci")
    async def run_ci(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Get CI going on the PR's head: add the CI gate label when the repository
        starts its full CI on one and the PR lacks it, else rerun the failed runs.
        The click is the person's approval; the policy still decides (pauses, the
        gate budget, the rerun limit), and the audit log records them."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        pr = await service.a.forge.get_pr(
            repo, number
        ) or await service.store.get_snapshot(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(409, f"{repo}#{number} is not open")
        plan = plan_run_ci(pr, profile.label_for("ci_gate"))
        if plan.state == "approval":
            # Approving runs the contributor's code on CI: it is the person's own.
            if not await service.can_post_as(user.login):
                raise HTTPException(
                    403, "sign in with GitHub, so the approval comes from you"
                )
            record = await _run_now(
                service,
                user,
                make_action(
                    ActionKind.CI_APPROVE_RUN,
                    repo,
                    ApproveRunsPayload(
                        run_ids=plan.approve_runs, workflows=plan.waiting
                    ),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator="approve-ci:"
                    + ",".join(str(r) for r in plan.approve_runs),
                    rationale=f"{user.login} approved running "
                    + ", ".join(plan.waiting),
                    draft_for=user.login,
                    provenance=Provenance(requested_by=user.login),
                ),
                as_them=True,
            )
            return {
                "plan": plan.model_dump(mode="json"),
                "actions": [
                    {
                        "id": record.action.id,
                        "kind": record.action.kind,
                        "status": record.status,
                        "error": record.error,
                    }
                ],
            }
        if not plan.can_run:
            raise HTTPException(
                409,
                {
                    "running": "CI is already running at this commit",
                    "passed": "CI passed at this commit; there is nothing to run",
                    "none": "no CI ran at this commit, and this repository starts it "
                    "on push, not on a label",
                }[plan.state],
            )
        actions = (
            [
                make_action(
                    ActionKind.LABEL_CI_GATE,
                    repo,
                    LabelPayload(roles=["ci_gate"]),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator="run-ci",
                    rationale=f"{user.login} pressed Run CI",
                    draft_for=user.login,
                )
            ]
            if plan.state == "gated"
            else [
                make_action(
                    ActionKind.CI_RERUN_FAILED,
                    repo,
                    RerunPayload(
                        run_id=run.run_id, workflow=run.workflow, attempt=run.attempt
                    ),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator=f"run-ci:{run.run_id}:{run.attempt or 1}",
                    rationale=f"{user.login} pressed Run CI",
                    draft_for=user.login,
                )
                for run in plan.reruns
            ]
        )
        results = [await _run_now(service, user, a) for a in actions]
        return {
            "plan": plan.model_dump(mode="json"),
            "actions": [
                {
                    "id": r.action.id,
                    "kind": r.action.kind,
                    "status": r.status,
                    "error": r.error,
                }
                for r in results
            ],
        }

    @r.get("/prs/{owner}/{name}/{number}/diff")
    async def pr_diff(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """The PR's diff at its current head, so drafts can show comments in place."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        pr = await service.store.get_snapshot(repo, number)
        if pr is None:
            raise HTTPException(404, f"{repo}#{number} is not known yet")
        key = (repo, number, pr.head_sha)
        text = _DIFFS.get(key)
        if text is None:
            text = (await service.a.forge.get_pr_diff(repo, number)) or ""
            text = text[:MAX_DIFF]
            _DIFFS[key] = text
            while len(_DIFFS) > 64:
                _DIFFS.pop(next(iter(_DIFFS)))
        return {"sha": pr.head_sha, "diff": text, "truncated": len(text) >= MAX_DIFF}

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
            pr,
            service.info.identities,
            profile.comments.legacy_markers,
            own_actions=await service.posted_as_people(repo),
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
        """Join the reviewers working on the PR; others who claimed it stay."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.store.add_assignee(
            PrAssignee(repo=repo, number=number, login=user.login, by=user.login)
        )
        return {"assignees": await _assignees(service, repo, number)}

    @r.delete("/prs/{owner}/{name}/{number}/claim")
    async def unclaim(
        request: Request,
        owner: str,
        name: str,
        number: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Leave the PR's reviewers, whether you claimed it or were assigned."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        await service.store.remove_assignee(repo, number, user.login)
        return {"assignees": await _assignees(service, repo, number)}

    @r.post("/prs/{owner}/{name}/{number}/assignees")
    async def assign(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: AssignBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Assign reviewers to the PR, for maintainers of the repository (design
        2.3). A login need not have signed in here, but it must be a GitHub user who
        can see the repository. With ``request_review``, their review is also
        requested on GitHub, as the repository's ``pr.request_reviewers`` rule
        allows."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if not await is_maintainer(service, user, repo):
            raise HTTPException(403, f"only maintainers of {repo} assign reviewers")
        pr = await service.store.get_snapshot(repo, number)
        if pr is None:
            raise HTTPException(404, f"{repo}#{number} is not known yet")
        logins: list[str] = []
        for raw in body.logins:
            try:
                login = check_login(raw)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if not (await service.a.forge.get_permission(repo, login)).at_least(
                Permission.READ
            ):
                raise HTTPException(
                    422, f"{login} is not a GitHub user who can see {repo}"
                )
            if login.lower() not in {x.lower() for x in logins}:
                logins.append(login)
        for login in logins:
            await service.store.add_assignee(
                PrAssignee(
                    repo=repo,
                    number=number,
                    login=login,
                    kind="assigned",
                    by=user.login,
                )
            )
        await service.store.add_audit(
            AuditEntry(
                kind="assign",
                repo=repo,
                number=number,
                actor=user.login,
                summary=f"assigned {', '.join(logins)}",
            )
        )
        requested: dict[str, Any] | None = None
        ask = [
            lg
            for lg in logins
            if lg.lower() != pr.author.lower()
            and lg.lower() not in {r.lower() for r in pr.requested_reviewers}
        ]
        if body.request_review and ask and pr.state == PrState.OPEN:
            record = await _run_now(
                service,
                user,
                make_action(
                    ActionKind.PR_REQUEST_REVIEWERS,
                    repo,
                    RequestReviewersPayload(logins=ask),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator=f"assign:{','.join(sorted(x.lower() for x in ask))}",
                    rationale=f"{user.login} assigned {', '.join(ask)}",
                    draft_for=user.login,
                    provenance=Provenance(requested_by=user.login),
                ),
            )
            requested = {
                "status": record.status,
                "error": record.error,
                "reason": record.decision.reason if record.decision else None,
                "action": record.action.id,
            }
        return {
            "assignees": await _assignees(service, repo, number),
            "requested": requested,
        }

    @r.delete("/prs/{owner}/{name}/{number}/assignees/{login}")
    async def unassign(
        request: Request,
        owner: str,
        name: str,
        number: int,
        login: str,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Take a reviewer off the PR: maintainers take anyone off, and everyone
        themselves. A review requested on GitHub stays requested there."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if login.lower() != user.login.lower() and not await is_maintainer(
            service, user, repo
        ):
            raise HTTPException(
                403, f"only maintainers of {repo} take other reviewers off a PR"
            )
        await service.store.remove_assignee(repo, number, login)
        return {"assignees": await _assignees(service, repo, number)}

    @r.get("/repos/{owner}/{name}/usage")
    async def usage(
        request: Request,
        owner: str,
        name: str,
        days: int = Query(default=30, ge=1, le=365),
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """What agents spent on the repository in the last ``days``, per person whose
        request they worked for (``login`` null for the service's own work, such as
        scans). Maintainers see everyone; others see the total and their own row."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        since = service.clock.now() - timedelta(days=days)
        runs = await service.store.list_agent_runs(USAGE_RUNS, repo=repo, since=since)
        jobs: dict[str, Job | None] = {}
        people: dict[str | None, dict[str, Any]] = {}
        total = _usage_row(None)
        for run in runs:
            who = run.requested_by
            if who is None and run.job_id:
                if run.job_id not in jobs:
                    jobs[run.job_id] = await service.store.get_job(run.job_id)
                job = jobs[run.job_id]
                who = _job_requester(job) if job else None
            _add_usage(total, run)
            key = who.lower() if who else None
            row = people.setdefault(key, _usage_row(who))
            _add_usage(row, run)
        everyone = await is_maintainer(service, user, repo)
        rows = sorted(
            people.values(),
            key=lambda r_: -(r_["input_tokens"] + r_["output_tokens"]),
        )
        if not everyone:
            rows = [
                r_ for r_ in rows if (r_["login"] or "").lower() == user.login.lower()
            ]
        return {
            "days": days,
            "since": since.isoformat(),
            "complete": len(runs) < USAGE_RUNS,
            "everyone": everyone,
            "total": total,
            "people": rows,
        }

    @r.get("/repos/{owner}/{name}/assignable")
    async def assignable(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Whether the user assigns reviewers in the repository, and the people to
        suggest: its People record's members, with their roles, then the logins
        assigned or claiming there before."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        people: list[dict[str, Any]] = []
        seen: set[str] = set()
        loaded = await service.governance.load(repo)
        if loaded.stored and not loaded.error:
            roles = {r.id: r.name for r in loaded.governance.roles}
            for m in loaded.governance.members:
                if m.login.lower() in seen:
                    continue
                seen.add(m.login.lower())
                people.append(
                    {
                        "login": m.login,
                        "role": roles.get(m.role, m.role),
                        "areas": m.areas,
                    }
                )
        for a in await service.store.list_assignees(repo):
            if a.login.lower() not in seen:
                seen.add(a.login.lower())
                people.append({"login": a.login, "role": None, "areas": []})
        return {
            "can_assign": await is_maintainer(service, user, repo),
            "people": people,
        }

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
        body: ReviewBody | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        return await _request_review(
            service, user, repo, number, body.agent if body else None
        )

    @r.post("/prs/{owner}/{name}/{number}/fix")
    async def request_fix(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: FixBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent fix the review's findings; the result is a diff that the
        maintainer opens as a PR into this PR's branch, as themselves."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        review = body.review
        if review is None:
            latest = await service.store.list_actions(
                repo=repo, kinds=[ActionKind.REVIEW_POST], limit=500
            )
            mine = [
                r
                for r in latest
                if r.action.number == number and r.action.payload.get("result")
            ]
            if not mine:
                raise HTTPException(409, "this PR has no review to fix yet")
            review = max(mine, key=lambda r: r.action.created_at).action.id
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            payload={
                "command": "fix",
                "by": user.login,
                "review": review,
                "findings": body.findings,
                **({"agent": body.agent} if body.agent else {}),
                **({"note": body.note} if body.note else {}),
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"repo": repo, "number": number, "event": event.id, "review": review}

    @r.post("/prs/{owner}/{name}/{number}/fix-ci")
    async def request_ci_fix(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: FixCiBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have an agent fix the PR's failing CI (``playbooks/fix_ci.py``); the result
        is a diff the person pushes as themselves. With ``lab``, the press also
        approves a session on that test machine, where the agent tests its fix: it is
        submitted as a ``lab.reproduce`` drafted for them and approved at once, so the
        repository's rule still decides (``off`` refuses it) and every command passes
        the machine's checks."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        pr = await service.a.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(409, f"{repo}#{number} is not open")
        failed = [c for c in pr.checks if c.failed]
        if not failed:
            raise HTTPException(409, "no CI job failed at this PR's head")
        session: dict[str, Any] | None = None
        if body.lab:
            profile = await service.profiles.active(repo)
            machine = service.machines.get(body.lab)
            binding = profile.labs.get(body.lab) if profile else None
            why = unusable(repo, body.lab, machine, binding)
            if why or machine is None:
                raise HTTPException(
                    409, why or f"there is no test machine {body.lab!r}"
                )
            names = ", ".join(sorted({c.name for c in failed}))
            record = await _run_now(
                service,
                user,
                make_action(
                    ActionKind.LAB_REPRODUCE,
                    repo,
                    LabReproducePayload(
                        lab=body.lab,
                        summary=f"Fix the failing CI of #{number} and test the fix",
                        steps=[
                            f"Read why these jobs fail: {names}",
                            "Fix the cause in the PR's code",
                            "Copy the fix to the machine and run the failing jobs' "
                            "steps there until they pass",
                        ],
                        needs_gpu=body.gpu,
                        timeout_seconds=time_limit(machine, binding),
                        sides=["head"],
                        requested_by=user.login,
                    ),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator=f"{FIX_CI_SESSION}{new_id('req')}",
                    rationale=f"{user.login} asked for a fix of the failing CI, "
                    f"tested on {body.lab}",
                    draft_for=user.login,
                    provenance=Provenance(requested_by=user.login),
                ),
            )
            session = {
                "action": record.action.id,
                "status": record.status,
                "reason": record.error
                or (record.decision.reason if record.decision else None),
            }
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            number=number,
            payload={
                "command": "fix_ci",
                "by": user.login,
                **({"agent": body.agent} if body.agent else {}),
                **({"note": body.note} if body.note else {}),
                **(
                    {"lab": body.lab, "lab_action": session["action"]}
                    if session
                    else {}
                ),
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"repo": repo, "number": number, "event": event.id, "session": session}

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
        maintains: dict[str, bool] = {}
        out = []
        for p in await service.store.list_proposals(state, repo):
            if p.repo not in visible:
                continue
            record = await service.store.get_action(p.action_id)
            if p.repo not in maintains:
                maintains[p.repo] = await is_maintainer(service, user, p.repo)
            out.append(
                {
                    **p.model_dump(mode="json"),
                    "action": record.model_dump(mode="json") if record else None,
                    "requester": proposal_requester(p, record),
                    "can_decide": await may_decide(
                        service, user, p, record, maintainer=maintains[p.repo]
                    ),
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
        await require_decide(service, user, proposal)
        if body.as_me and not await service.can_post_as(user.login):
            raise HTTPException(
                409, "sign in again with GitHub so the service can post as you"
            )
        try:
            proposal = await service.pipeline.approve(
                proposal_id,
                user.login,
                body.payload,
                post_as=user.login if body.as_me else None,
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return proposal.model_dump(mode="json")

    @r.post("/proposals/{proposal_id}/edit")
    async def save_edits(
        request: Request,
        proposal_id: str,
        body: EditBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Save edits to a pending draft without posting it."""
        service = service_of(request)
        proposal = await service.store.get_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(404, "unknown proposal")
        await require_decide(service, user, proposal)
        try:
            proposal = await service.pipeline.save_edits(
                proposal_id, user.login, body.payload
            )
        except ValueError as e:  # a stale proposal, or a payload that does not fit
            raise HTTPException(409, str(e)) from e
        record = await service.store.get_action(proposal.action_id)
        return {
            **proposal.model_dump(mode="json"),
            "action": record.model_dump(mode="json") if record else None,
        }

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
        await require_decide(service, user, proposal)
        try:
            proposal = await service.pipeline.reject(
                proposal_id, user.login, body.reason
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return proposal.model_dump(mode="json")

    # -- the service's own code ---------------------------------------------------------

    @r.get("/service/logs")
    async def service_logs(
        request: Request,
        lines: int = Query(default=300, ge=1, le=5000),
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """The end of the log ``am start`` keeps (``<data>/logs/service.log``), for
        maintainers of the service's repository; anything that looks like a
        credential is removed."""
        service = service_of(request)
        allowed, why = await code_maintainer(service, user)
        if not allowed:
            raise HTTPException(403, why)
        path = Path(request.app.state.log_path)
        if not path.exists():
            return {"path": str(path), "lines": [], "kept": False}
        with path.open(encoding="utf-8", errors="replace") as f:
            last = [line.rstrip("\n") for line in deque(f, maxlen=lines)]
        return {
            "path": str(path),
            "lines": [redact_secrets(line)[0] for line in last],
            "kept": True,
        }

    @r.get("/service")
    async def service_status(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        repo = service.settings.code_repo
        if repo is None:
            return {"code_repo": None}
        perm = await service.a.forge.get_permission(repo, user.login)
        if not (
            perm.at_least(Permission.READ) or user.login in service.settings.operators
        ):
            raise HTTPException(
                403, f"you need access to {repo} to see the service's code"
            )
        allowed, reason = await code_maintainer(service, user)
        status = await service.a.updater.status() if service.a.updater else None
        prs = [
            {
                "number": pr.number,
                "title": pr.title,
                "author": pr.author,
                "branch": pr.head_ref,
                "url": pr.url,
                "head_sha": pr.head_sha,
                "self_improvement": pr.head_ref.startswith("am/self/"),
            }
            for pr in await service.a.forge.list_open_prs(repo)
        ]
        actions = await service.store.list_actions(
            repo=repo,
            kinds=[
                ActionKind.CODE_PR,
                ActionKind.CODE_MERGE,
                ActionKind.SERVICE_DEPLOY,
            ],
        )
        return {
            "code_repo": repo,
            "branch": service.settings.code_branch,
            "approve_permission": service.settings.code_approver,
            "can_approve": allowed,
            "reason": reason,
            "status": status.model_dump(mode="json") if status else None,
            "prs": prs,
            "history": [
                {
                    "kind": a.action.kind,
                    "status": a.status,
                    "summary": a.action.rationale,
                    "result": a.external_ref,
                    "error": a.error,
                    "at": a.updated_at.isoformat(),
                }
                for a in actions[:20]
            ],
        }

    @r.post("/service/prs/{number}/merge")
    async def merge_service_pr(
        request: Request, number: int, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        allowed, reason = await code_maintainer(service, user)
        if not allowed:
            raise HTTPException(403, reason)
        repo = service.settings.code_repo
        assert repo is not None
        pr = await service.a.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            raise HTTPException(404, f"{repo}#{number} is not an open PR")
        action = make_action(
            ActionKind.CODE_MERGE,
            repo,
            CodeMergePayload(number=number, approved_by=user.login),
            number=number,
            head_sha=pr.head_sha,
        )
        action.provenance.requested_by = user.login
        action.rationale = f"{user.login} approved merging {repo}#{number}"
        record = await service.pipeline.submit(action)
        return record.model_dump(mode="json")

    @r.post("/service/deploy")
    async def deploy(
        request: Request, body: DeployBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        allowed, reason = await code_maintainer(service, user)
        if not allowed:
            raise HTTPException(403, reason)
        if service.a.updater is None:
            raise HTTPException(
                409, "this service does not run from a git checkout it can update"
            )
        repo = service.settings.code_repo
        assert repo is not None
        action = make_action(
            ActionKind.SERVICE_DEPLOY,
            repo,
            ServiceDeployPayload(sha=body.sha, approved_by=user.login),
            discriminator=f"{body.sha}:{service.clock.now().timestamp()}",
        )
        action.provenance.requested_by = user.login
        action.rationale = f"{user.login} approved deploying {body.sha[:12]}"
        record = await service.pipeline.submit(action)
        return record.model_dump(mode="json")

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
            {
                **c.model_dump(mode="json"),
                "profiles": await _profile_changes(service, c),
            }
            for c in await service.store.list_memory_changes(state)
            if set(c.repos) <= visible and c.repos
        ]

    @r.get("/memory/changes/{change_id}")
    async def memory_change(
        request: Request, change_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """One memory change in any state, for cards that outlive its approval."""
        service = service_of(request)
        change = await service.store.get_memory_change(change_id)
        if change is None:
            raise HTTPException(404, "no such memory change")
        if change.service:
            if not (await service.service_gate(user.login)).allowed:
                raise HTTPException(404, "no such memory change")
        elif not change.repos or not set(change.repos) <= set(
            await visible_repos(service, user)
        ):
            raise HTTPException(404, "no such memory change")
        return {
            **change.model_dump(mode="json"),
            "profiles": await _profile_changes(service, change),
        }

    @r.post("/memory/changes/{change_id}/approve")
    async def approve_memory(
        request: Request, change_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        try:
            decisions, state = await _merge_memory(
                service.approve_memory_change(change_id, user.login)
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
            async for event in service.bus.stream(service.closing):
                if await request.is_disconnected():
                    break
                if event is None:
                    yield ": ping\n\n"
                    continue
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
                    data["asked_for"] = event.payload.get("asked_for")
                if event.kind == EventKind.JOB_PROGRESS:
                    data["job"] = event.payload.get("job")
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
    service: Service, user: User, repo: str, number: int, agent: str | None = None
) -> dict[str, Any]:
    """Publish a review request for one PR and say what it will run.

    One review per PR, head commit, and agent: when the same agent is already
    reviewing this commit, or left a draft for it, the request joins that review
    instead of starting another, whoever asked first. A different agent may review
    the same commit.
    """
    pr = await service.store.get_snapshot(repo, number)
    profile = await service.profiles.active(repo)
    out: dict[str, Any] = {"repo": repo, "number": number}
    if pr is None or profile is None:
        return {
            **out,
            "run": "skip",
            "reason": "unknown PR or repository without a profile",
        }
    ids = service.info.identities | set(profile.people.review_identities)
    state = classify_review_state(
        pr,
        ids,
        profile.comments.legacy_markers,
        include_drafts=True,
        own_actions=await service.posted_as_people(repo),
    ).state
    if state == "UP_TO_DATE":
        return {**out, "run": "skip", "reason": "already reviewed at the current head"}
    run = (
        "followup" if state in ("FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS") else "review"
    )
    route = profile.route(run, service.settings.routing)
    default_agent = service.runs_on(route)
    chosen = agent or default_agent
    known = getattr(service.a.agents, "runtimes", None)
    if agent and known is not None and agent not in known:
        return {**out, "run": "skip", "reason": f"no enabled agent named {agent!r}"}
    if agent and not service.agent_ready(agent):
        return {
            **out,
            "run": "skip",
            "reason": f"{agent} is not installed on this host",
        }
    head = pr.head_sha[:12]
    active = [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING]
    for job in await service.store.list_jobs(active, repo=repo):
        if job.playbook not in ("review", "followup") or job.number != number:
            continue
        last = job.events[-1] if job.events else {}
        payload = last.get("payload") or {}
        if (last.get("sha") or "")[:12] == head and (
            payload.get("agent") or default_agent
        ) == chosen:
            return {
                **out,
                "run": "joined",
                "by": payload.get("by"),
                "agent": chosen,
                "state": state,
            }
    for proposal in await service.store.list_proposals(ProposalState.PENDING, repo):
        record = await service.store.get_action(proposal.action_id)
        if (
            record is not None
            and record.action.kind == ActionKind.REVIEW_POST
            and record.action.number == number
            and (record.action.head_sha or "")[:12] == head
            and record.action.payload.get("runtime") == chosen
        ):
            return {
                **out,
                "run": "drafted",
                "by": proposal.requested_by,
                "agent": chosen,
                "proposal": proposal.id,
                "state": state,
            }
    event = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=repo,
        number=number,
        sha=pr.head_sha,
        payload={
            "command": "review",
            "by": user.login,
            "source": "dashboard",
            **({"agent": agent} if agent else {}),
        },
        source="dashboard",
    )
    await service.bus.publish(event)
    return {**out, "run": run, "event": event.id, "state": state, "agent": chosen}


async def review_qualities(service: Service, repo: str) -> dict[int, ReviewQuality]:
    """Per reviewed PR of ``repo``: its quality as its latest review found it."""
    by_pr: dict[int, list[ActionRecord]] = {}
    for record in await service.store.list_actions(
        repo=repo, kinds=[ActionKind.REVIEW_POST], limit=5000
    ):
        if record.action.number is not None:
            by_pr.setdefault(record.action.number, []).append(record)
    out = {}
    for number, records in by_pr.items():
        quality = review_quality(
            records, await assessed_quality(service.store, repo, number)
        )
        if quality is not None:
            out[number] = quality
    return out


async def review_activity(service: Service, repo: str) -> dict[int, dict[str, Any]]:
    """Per PR: the latest review or follow-up job, a pending draft, and the last posted review."""
    out: dict[int, dict[str, Any]] = {}
    for job in await service.store.list_jobs(repo=repo, limit=500):
        if job.playbook not in ("review", "followup") or job.number is None:
            continue
        row = out.setdefault(job.number, {})
        if "job" not in row:
            payload = (job.events[-1].get("payload") if job.events else None) or {}
            row["job"] = {
                "id": job.id,
                "playbook": job.playbook,
                "status": job.status,
                "updated_at": job.updated_at.isoformat(),
                "result": job.result,
                "error": job.error,
                "by": payload.get("by"),
                "agent": payload.get("agent"),
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
