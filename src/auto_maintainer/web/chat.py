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

"""Chat with the service (design 5.13).

Each thread belongs to one maintainer. The agent sees the maintainer's queue and
the memory index and may propose actions; every proposed action becomes a
proposal the maintainer approves in the thread, whatever the policy says.

When the maintainer asks for something the service cannot do yet, the agent proposes
``improve``: the ``self_improve`` playbook then changes the service's own code and
opens a PR on its repository, which a maintainer of that repository approves before it
is merged and deployed (design 5.12).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.actions import (
    ActionKind,
    CommentUpsertPayload,
    LabelPayload,
    MemoryChangePayload,
    NudgePayload,
    RequestReviewersPayload,
    RerunPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import check_repo, new_id
from ..domain.memory import (
    MemoryChange,
    Signal,
    SignalKind,
    Tier,
    profile_path,
    repo_dir,
)
from ..domain.records import ChatMessage, ChatRequest, ChatThread, JobStatus
from ..domain.review import review_quality
from ..domain.secrets import redact_secrets
from ..domain.settings import (
    PROVIDER_PROTOCOLS,
    SettingsError,
    effective_agents,
    effective_routing,
    secret_names,
)
from ..playbooks.prompts import schema_hint, untrusted
from ..playbooks.quality import assessed_quality, quick_quality
from ..profiles.layers import ProfileError, parse_yaml
from .api import (
    code_maintainer,
    require_view,
    review_setup,
    service_of,
    start_evaluations,
    visible_repos,
)
from .auth import User, current_user

log = logging.getLogger(__name__)


class ProposedAction(BaseModel):
    kind: Literal[
        "label",
        "rerun",
        "request_reviewers",
        "comment",
        "nudge",
        "review",
        "evaluate",
        "lesson",
        "improve",
        "add_repo",
        "configure",
        "governance",
    ]
    repo: str = Field(
        default="",
        description="The repository (owner/name); empty for kind=improve. For kind=add_repo, the repository to onboard.",
    )
    number: int | None = Field(
        default=None,
        description="The PR; for kind=evaluate, empty means every open PR of the repository.",
    )
    labels: list[str] = Field(default_factory=list)
    run_id: int | None = None
    logins: list[str] = Field(default_factory=list)
    body: str = ""
    lesson: str = Field(
        default="",
        description="For kind=lesson: a review lesson to remember for this repository.",
    )
    request: str = Field(
        default="",
        description=(
            "For kind=improve: the new behavior or fix the service's own code needs, "
            "precise enough to implement and test. For kind=governance: what to change "
            "in the repository's People record, or where its process is written."
        ),
    )
    people_mode: Literal["edit", "members"] = Field(
        default="edit",
        description=(
            "For kind=governance: edit the record, or check who should change role "
            "under it (members)."
        ),
    )
    settings_yaml: str = Field(
        default="",
        description=(
            "For kind=configure: the complete new document. With repo empty it replaces "
            "the service settings (config/service.yaml); with repo set it replaces that "
            "repository's settings layer. Keep everything you were not asked to change."
        ),
    )
    summary: str = Field(
        default="",
        description="For kind=configure: one line saying what the change does.",
    )


class Lookup(BaseModel):
    """A record the agent asks the service for before it answers (the service guide's
    "Diagnosing" section says which to read for what)."""

    kind: Literal["pr_record", "repo_failures", "service_log"] = Field(
        description="pr_record: every action on one PR with its status, error, "
        "proposal, and jobs. repo_failures: the repository's recent actions that "
        "failed, were refused, or were superseded. service_log: the service's log "
        "lines that contain `text`, such as an action or job id."
    )
    repo: str = ""
    number: int | None = None
    text: str = Field("", description="For service_log: what the lines contain.")


class ChatAnswer(BaseModel):
    reply: str
    proposals: list[ProposedAction] = Field(default_factory=list)
    lookups: list[Lookup] = Field(
        default_factory=list,
        description="Records to read before answering. With lookups, leave proposals "
        "empty and reply with one line on what you check: the service sends the "
        "results back and you answer then.",
    )


class PageContext(BaseModel):
    """What the maintainer is looking at when they ask the agent from a page.

    Only identifiers come from the browser; the service reads the page's data itself,
    so the agent sees what the maintainer may see and nothing a page could invent.
    """

    page: str = Field(default="home", max_length=40)
    repo: str | None = None
    number: int | None = None


class MessageBody(BaseModel):
    text: str
    repo: str | None = None
    context: PageContext | None = None


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/chat")

    @r.get("/threads")
    async def threads(
        request: Request, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        return [
            t.model_dump(mode="json")
            for t in await service.store.list_threads(user.login)
            if t.job_id is None and t.topic is None
        ]

    @r.post("/threads")
    async def new_thread(
        request: Request, body: MessageBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        service = service_of(request)
        if body.repo:
            await require_view(service, user, body.repo)
        repo = body.repo or (body.context.repo if body.context else None)
        if repo:
            await require_view(service, user, repo)
        number = (
            body.context.number
            if body.context and body.context.repo == repo and repo
            else None
        )
        thread = ChatThread(
            owner=user.login, title=body.text[:80], repo=repo, number=number
        )
        await service.store.save_thread(thread)
        return thread.model_dump(mode="json")

    @r.get("/threads/{thread_id}/messages")
    async def messages(
        request: Request, thread_id: str, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None or thread.owner != user.login:
            raise HTTPException(404, "unknown thread")
        return [
            m.model_dump(mode="json")
            for m in await service.store.list_messages(thread_id)
        ]

    @r.get("/requests")
    async def pending_requests(
        request: Request, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """Request cards waiting in any conversation that ``user`` may decide (an
        ``improve`` for the owner of this service, an ``add_repo`` for
        those who may configure that repository), and their own."""
        service = service_of(request)
        out = []
        for message in await service.store.messages_with_pending_requests():
            thread = await service.store.get_thread(message.thread_id)
            if thread is None:
                continue
            for req in message.requests:
                if req.state != "pending":
                    continue
                allowed, _ = await _may_decide(service, user, req)
                if allowed or thread.owner == user.login:
                    out.append(
                        {
                            "thread_id": thread.id,
                            "message_id": message.id,
                            "asked_by": thread.owner,
                            "created_at": message.created_at.isoformat(),
                            "can_decide": allowed,
                            "request": req.model_dump(mode="json"),
                        }
                    )
        return out

    @r.post("/threads/{thread_id}/requests/{request_id}/{decision}")
    async def decide_request(
        request: Request,
        thread_id: str,
        request_id: str,
        decision: Literal["approve", "decline"],
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Approve or decline a request card. Approving needs the right the card
        asks for (``_may_decide``); whoever asked may also decline their own.
        Nothing starts before approval."""
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None:
            raise HTTPException(404, "unknown thread")
        history = await service.store.list_messages(thread_id)
        found = next(
            ((m, r) for m in history for r in m.requests if r.id == request_id),
            None,
        )
        if found is None:
            raise HTTPException(404, "no such request in this conversation")
        message, req = found
        allowed, why = await _may_decide(service, user, req)
        own = thread.owner == user.login
        if not own and not allowed:
            raise HTTPException(404, "no such request in this conversation")
        if req.state != "pending":
            raise HTTPException(409, f"this request was already {req.state}")
        if decision == "approve":
            if not allowed:
                raise HTTPException(403, why)
            if req.kind == "improve":
                running = await _running_improvement(service, thread_id)
                req.job = running or await _start_improve(
                    service, thread.owner, user.login, req.text, thread_id, history
                )
            else:
                try:
                    await service.add_repo(check_repo(req.text), user.login)
                except ValueError as e:
                    raise HTTPException(409, str(e)) from e
        req.state = "approved" if decision == "approve" else "declined"
        req.decided_by = user.login
        await service.store.save_message(message)
        return message.model_dump(mode="json")

    @r.get("/threads/{thread_id}/steps")
    async def steps(
        request: Request,
        thread_id: str,
        run: str | None = None,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """What the agent did for one answer (``run``), or is doing for the newest
        message when ``run`` is omitted."""
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None or thread.owner != user.login:
            raise HTTPException(404, "unknown thread")
        if run is None:
            run = thread.live_run
        elif run != thread.live_run and not any(
            m.run_id == run for m in await service.store.list_messages(thread_id)
        ):
            raise HTTPException(404, "no such run in this conversation")
        if run is None:
            return {"run": None, "live": False, "steps": []}
        found = await service.progress.steps(run)
        return {
            "run": run,
            "live": run == thread.live_run,
            "steps": [s.model_dump(mode="json") for s in found],
        }

    @r.post("/threads/{thread_id}/messages")
    async def post(
        request: Request,
        thread_id: str,
        body: MessageBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_of(request)
        thread = await service.store.get_thread(thread_id)
        if thread is None or thread.owner != user.login:
            raise HTTPException(404, "unknown thread")
        repos = await visible_repos(service, user)
        text, removed = redact_secrets(body.text)
        await service.store.add_message(
            ChatMessage(thread_id=thread_id, role="user", text=text)
        )
        history = await service.store.list_messages(thread_id)
        queue = []
        for name in repos[:10]:
            for ev in (await service.store.list_evaluations(name))[:15]:
                queue.append(
                    f"{name}#{ev.number} [{ev.tier} {ev.total}] {ev.title} — {ev.summary}"
                )
        memory_index = await service.knowledge.memory_index(thread.repo)
        workdir, where = await _workdir(service, thread)
        code_dir = await _service_code(service, thread)
        if code_dir is not None:
            where += (
                f"; and, read-only at {code_dir}, the service's own code at the commit "
                "it runs: read it to see how the service does something"
            )
        prompt = "\n\n".join(
            [
                f"You are the maintainers' assistant for {', '.join(repos) or 'no repositories yet'}. You are talking to {user.login}. "
                "Answer from the data below. You cannot act directly: put any change you suggest in `proposals`. "
                "Each proposal appears as a card under your answer, and they differ in what happens next: actions on a PR "
                "(label, comment, rerun, ...) and configuration changes wait for the maintainer to approve the card; "
                "review and evaluate (score a PR's priority again, or every open PR's when number is empty, with "
                "the repository's lessons as they are now) start at once; "
                "a lesson is saved at once when this maintainer may configure the repository, and otherwise waits for one "
                "who may; an improve request and add_repo (onboard a repository: the service reads it and drafts its "
                "profile for its maintainers to approve) wait on a request card for a person who may approve them. Say "
                "which of these happens; never ask the maintainer to approve a lesson they may save. Internal priorities "
                "are private: never put them in public text. Before proposing improve, check the service guide below: "
                "if the dashboard or a proposal already does what is asked, say how instead. When the maintainer asks for "
                "something the service cannot do yet, or for a change in how it works that needs code rather than a "
                "setting or a lesson, propose kind=improve with the request spelled out: once the owner of this "
                "service approves the card, the service changes its own code in the background and opens a "
                "PR on its own repository, which the owner reviews before it is merged and deployed. A conversation "
                "runs one such change at a time: if the history shows one pending or running, do not propose it again.",
                await _guide(service),
                f"Your working directory is {where}.",
                f"Memory index:\n{memory_index or '(empty)'}",
                "Review queue (internal):\n" + ("\n".join(queue) or "(empty)"),
                await _page_block(service, user, body.context, repos),
                await _settings_block(service, user),
                untrusted(
                    "chat.history",
                    "\n".join(f"{m.role}: {m.text}" for m in history[-20:]),
                    30_000,
                ),
                schema_hint(ChatAnswer.model_json_schema()),
            ]
        )
        route = service.settings.routing.get("chat")
        if route is None:
            raise HTTPException(500, "no chat route is configured")
        run = new_id("run")
        task = AgentTask(
            kind=TaskKind.CHAT,
            prompt=prompt,
            workdir=workdir,
            output_schema=ChatAnswer.model_json_schema(),
            resume_session=thread.session,
            job_id=run,
            requested_by=user.login,
            reference_dirs=[code_dir] if code_dir is not None else [],
        )
        thread.live_run = run
        await service.store.save_thread(thread)
        try:
            for turn in range(LOOKUP_TURNS + 1):
                result = await service.a.agents.run(task, route, ChatAnswer)
                if not result.ok or result.structured is None:
                    raise HTTPException(502, f"the chat agent failed: {result.error}")
                answer = ChatAnswer.model_validate(result.structured)
                if not answer.lookups or turn == LOOKUP_TURNS:
                    break
                found = [
                    await _lookup(service, user, q, repos, request.app.state.log_path)
                    for q in answer.lookups[:MAX_LOOKUPS]
                ]
                follow = "\n\n".join(
                    [
                        "Results of your lookups:\n" + "\n\n".join(found),
                        "Answer now, from these results; no more lookups."
                        if turn == LOOKUP_TURNS - 1
                        else "Answer now, or ask for more lookups if these do not "
                        "settle it.",
                        schema_hint(ChatAnswer.model_json_schema()),
                    ]
                )
                task = task.model_copy(
                    update={
                        # A resumed session holds the first prompt; else send it again.
                        "prompt": follow
                        if result.session
                        else prompt + "\n\n" + follow,
                        "resume_session": result.session,
                    }
                )
        finally:
            thread.live_run = None
            await service.store.save_thread(thread)
        thread.session = result.session or thread.session
        thread.runtime = result.runtime
        await service.store.save_thread(thread)
        created: list[str] = []
        changes: list[str] = []
        jobs: list[str] = []
        requests: list[ChatRequest] = []
        notes: list[str] = []
        if removed:
            notes.append(
                "I removed what looked like a credential from your message before "
                "reading it. Set API keys in the key field on the Settings page: they "
                "stay on the service host and never pass through a conversation."
            )
        for p in answer.proposals:
            if p.kind == "improve":
                if any(r.kind == "improve" for r in requests):
                    notes.append(
                        "I asked for one change to the service's code at a time; "
                        "the first request is below."
                    )
                    continue
                job, note = await _improve_request(service, p, thread_id)
                if job:
                    jobs.append(job)
                elif not note:
                    requests.append(ChatRequest(kind="improve", text=p.request.strip()))
                notes += [note] if note else []
                continue
            if p.kind == "add_repo":
                try:
                    requests.append(
                        ChatRequest(kind="add_repo", text=check_repo(p.repo))
                    )
                except ValueError as e:
                    notes.append(str(e))
                continue
            if p.kind == "lesson":
                if p.repo in repos and p.lesson.strip():
                    changes.append(await save_lesson(service, user, p))
                continue
            if p.kind == "evaluate":
                if p.repo in repos:
                    notes.append(await _evaluate(service, user, p))
                continue
            if p.kind == "governance":
                if p.repo in repos:
                    jobs.append(await _governance(service, user, p))
                continue
            if p.kind == "configure":
                change_id, problem = await _configure(service, user, p, repos)
                if change_id:
                    changes.append(change_id)
                if problem:
                    notes.append(problem)
                continue
            if p.repo not in repos:
                continue
            created += await _propose(service, user, p)
        reply = "\n\n".join([answer.reply, *notes]) if notes else answer.reply
        message = ChatMessage(
            thread_id=thread_id,
            role="assistant",
            text=reply,
            proposals=created,
            changes=changes,
            jobs=jobs,
            requests=requests,
            run_id=run,
        )
        await service.store.add_message(message)
        return message.model_dump(mode="json")

    return r


async def _workdir(service: Any, thread: ChatThread) -> tuple[Path, str]:
    """Where the agent works for this conversation, and what that is, kept for the
    whole conversation: a checkout of its repository (at the PR's head when it started
    on a PR); without a repository, a checkout of the service's own code at the commit
    it runs, so questions about the service are answered from its code; without
    either, a scratch folder of its own."""
    if thread.workdir and Path(thread.workdir).is_dir():
        return Path(thread.workdir), _where(service, thread)
    path: Path | None = None
    target = thread.repo or service.settings.code_repo
    if target:
        try:
            info = await service.a.forge.get_repo(target)
            pr = (
                await service.a.forge.get_pr(target, thread.number)
                if thread.repo and thread.number
                else None
            )
            sha = pr.head_sha if pr else info.default_branch_sha
            if not thread.repo and service.a.updater is not None:
                # The code the service runs, which may lag its default branch.
                sha = (await service.a.updater.status()).running_sha or sha
            if sha:
                path = await service.a.workspace.prepare(
                    target, pr.number if pr else None, sha, info.default_branch
                )
        except Exception as e:  # the conversation still works without the code
            log.warning("no checkout of %s for chat: %s", target, e)
    if path is None:
        path = service.settings.scratch_dir / "chat" / thread.id
        path.mkdir(parents=True, exist_ok=True)
    if thread.workdir and thread.workdir != str(path):
        thread.session = None  # the old checkout is gone; its session cannot resume
    thread.workdir = str(path)
    await service.store.save_thread(thread)
    return path, _where(service, thread)


async def _service_code(service: Any, thread: ChatThread) -> Path | None:
    """For a conversation on a repository, a checkout of the service's own code at
    the commit it runs, kept for the whole conversation and given to the agent
    read-only, so it can read how the service does what it is asked about. A fresh
    worktree from the service's mirror, never the checkout the service runs from,
    whose git settings may hold credentials. ``None`` without a service repository
    or when it cannot be checked out."""
    repo = service.settings.code_repo
    if not thread.repo or not repo:
        return None
    if thread.code_dir and Path(thread.code_dir).is_dir():
        return Path(thread.code_dir)
    try:
        info = await service.a.forge.get_repo(repo)
        sha = info.default_branch_sha
        if service.a.updater is not None:
            sha = (await service.a.updater.status()).running_sha or sha
        path = await service.a.workspace.prepare(repo, None, sha, info.default_branch)
    except Exception as e:  # the conversation works without it
        log.warning("no checkout of %s for chat: %s", repo, e)
        return None
    thread.code_dir = str(path)
    await service.store.save_thread(thread)
    return Path(path)


def _where(service: Any, thread: ChatThread) -> str:
    """The conversation's working directory, in words for the agent."""
    if thread.repo:
        on = f" at the head of PR #{thread.number}" if thread.number else ""
        return f"a checkout of {thread.repo}{on}"
    if service.settings.code_repo:
        return (
            f"a checkout of the service's own code ({service.settings.code_repo}) at the "
            "commit it runs: read it to answer questions about the service itself; "
            "changing it changes nothing that runs"
        )
    return "an empty folder of this conversation's own"


RECORD_ACTIONS = 40
"""The most recent actions of a PR the chat agent sees, with how each ended."""
RECORD_JOBS = 12


def _draft_summary(payload: dict[str, Any]) -> str:
    """A review draft in a line: its kind and where each inline comment is, to check
    against the PR's diff."""
    comments = payload.get("comments") or []
    places = [
        f"{c.get('path')}:"
        + (
            f"{c['start_line']}-{c.get('line')}"
            if c.get("start_line")
            else str(c.get("line"))
        )
        + ("" if c.get("side", "RIGHT") == "RIGHT" else " (old side)")
        for c in comments
    ]
    return f"draft: {payload.get('event', 'COMMENT')} review, " + (
        f"{len(places)} inline comments at " + ", ".join(places)
        if places
        else "no inline comments"
    )


async def _service_record(service: Any, repo: str, number: int) -> str:
    """The service's own record of ``repo#number``, oldest first: each action it
    proposed or took, when it last changed, how it ended (status, error, the
    policy's rule), the proposal behind it (who decided, why, its warnings), for a
    review draft its kind and where its inline comments are, and its recent jobs
    (the ``pr_record`` lookup)."""
    actions = await service.store.list_actions(
        repo=repo, number=number, limit=RECORD_ACTIONS
    )
    lines = []
    for record in sorted(actions, key=lambda r: r.action.created_at):
        a = record.action
        parts = [
            f"{a.created_at:%Y-%m-%d %H:%M} UTC {a.kind} "
            f"at {(a.head_sha or '-')[:7]}: {record.status}"
        ]
        if record.decision is not None:
            parts.append(f"policy {record.decision.kind} ({record.decision.rule})")
        proposal = await service.store.proposal_for_action(a.id)
        if proposal is not None:
            parts.append(
                f"proposal {proposal.state}"
                + (f" for {proposal.requested_by}" if proposal.requested_by else "")
                + (f", decided by {proposal.decided_by}" if proposal.decided_by else "")
            )
            if proposal.reason:
                parts.append(f"reason: {proposal.reason}")
            if proposal.warnings:
                parts.append("warnings: " + "; ".join(proposal.warnings))
        if record.post_as:
            parts.append(f"as {record.post_as}")
        if a.kind in (ActionKind.REVIEW_POST, ActionKind.REVIEW_UPDATE):
            parts.append(_draft_summary(a.payload))
        if record.updated_at and record.updated_at > a.created_at:
            parts.append(f"last changed {record.updated_at:%Y-%m-%d %H:%M} UTC")
        if record.error:
            parts.append(f"error: {record.error}")
        lines.append(" | ".join(parts))
    jobs = [j for j in await service.store.list_jobs(repo=repo) if j.number == number]
    for job in sorted(jobs, key=lambda j: j.updated_at)[-RECORD_JOBS:]:
        lines.append(
            f"{job.updated_at:%Y-%m-%d %H:%M} UTC job {job.playbook}: {job.status}"
            + (f" | error: {job.error}" if job.error else "")
        )
    return "\n".join(lines) or "nothing yet"


LOOKUP_TURNS = 2
"""How many times one answer may ask for lookups before it must answer."""
MAX_LOOKUPS = 4
LOG_LINES = 5000
"""How far back in the service's log a ``service_log`` lookup reads."""
LOG_MATCHES = 60


async def _lookup(
    service: Any, user: User, q: Lookup, repos: list[str], log_path: Path | None
) -> str:
    """Run one lookup the agent asked for, with ``user``'s permissions: records of a
    repository they see; the service's log only for operators and maintainers of the
    service's repository. The answer is redacted of credentials and marked as data."""
    target = f"{q.repo}#{q.number}" if q.number else q.repo
    what = " ".join(x for x in (q.kind, target, q.text) if x)
    if q.kind in ("pr_record", "repo_failures"):
        if q.repo not in repos:
            return f"{what}: not a repository {user.login} can see"
        if q.kind == "pr_record":
            if q.number is None:
                return f"{what}: pr_record needs a number"
            text = await _service_record(service, q.repo, q.number)
        else:
            text = await _repo_failures(service, q.repo)
    else:
        allowed = (
            user.login in service.settings.operators
            or (await service.service_gate(user.login)).allowed
        )
        if not allowed:
            return f"{what}: only operators and the owner of this service read its log"
        if not q.text.strip() or log_path is None:
            return f"{what}: say what the lines contain"
        text = await asyncio.to_thread(_log_lines, log_path, q.text.strip())
    return f"{what}:\n" + untrusted(f"lookup.{q.kind}", redact_secrets(text)[0], 16000)


async def _repo_failures(service: Any, repo: str) -> str:
    """The repository's latest actions that did not run as planned."""
    statuses = ["failed", "denied", "superseded"]
    records = await service.store.list_actions(repo=repo, statuses=statuses, limit=30)
    lines = [
        f"{r.action.created_at:%Y-%m-%d %H:%M} UTC {r.action.kind} "
        f"#{r.action.number}: {r.status}" + (f" | error: {r.error}" if r.error else "")
        for r in sorted(records, key=lambda r: r.action.created_at)
    ]
    return "\n".join(lines) or "nothing failed recently"


def _log_lines(path: Path, needle: str) -> str:
    """The service log's latest lines that contain ``needle``, ignoring case."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-LOG_LINES:]
    except OSError:
        return "the service's log cannot be read"
    low = needle.lower()
    hits = [ln.rstrip() for ln in tail if low in ln.lower()]
    return "\n".join(hits[-LOG_MATCHES:]) or f"no recent line contains {needle!r}"


async def _page_block(
    service: Any, user: User, context: PageContext | None, repos: list[str]
) -> str:
    """The page the maintainer asked from, read on the server side."""
    if context is None:
        return "Current page: the conversations page."
    lines = [f"Current page: {context.page}."]
    if context.repo and context.repo not in repos:
        return lines[0] + " (a repository you cannot see)"
    if context.repo and context.number is not None:
        pr = await service.store.get_snapshot(
            context.repo, context.number
        ) or await service.a.forge.get_pr(context.repo, context.number)
        if pr is None:
            return lines[0] + f" {context.repo}#{context.number} is not known."
        ev = await service.store.get_evaluation(context.repo, context.number)
        lines += [
            f"The maintainer is looking at {pr.repo}#{pr.number} by {pr.author} "
            f"({pr.state.value.lower()}{', draft' if pr.is_draft else ''}), head {pr.head_sha[:12]}, "
            f"base {pr.base_ref}, +{pr.additions}/-{pr.deletions} in {len(pr.files)} files, "
            f"labels: {', '.join(pr.labels) or 'none'}.",
            "Checks: "
            + (
                ", ".join(
                    f"{c.name}={(c.conclusion or c.status).lower()}"
                    for c in pr.checks[:20]
                )
                or "none"
            )
            + ".",
        ]
        if ev is not None:
            lines.append(f"Internal evaluation: {ev.tier} ({ev.total}); {ev.summary}")
        quality = review_quality(
            await service.store.list_actions(
                repo=pr.repo,
                number=pr.number,
                kinds=[ActionKind.REVIEW_POST],
                limit=500,
            ),
            await assessed_quality(service.store, pr.repo, pr.number),
        )
        if quality is not None:
            lines.append(
                f"Internal quality, as the reviewing agent judged it at {quality.sha[:12]} "
                f"by the repository's quality guide: {quality.score}/100 ({quality.band}). "
                f"{quality.reason}".rstrip()
            )
        quick = await quick_quality(service.store, pr.repo, pr.number)
        if quick is not None:
            lines.append(
                f"Internal quick quality score from the diff at {quick.sha[:12]}, before "
                f"any review{' (the review score above is the accurate one)' if quality else ''}: "
                f"{quick.score}/100 ({quick.band}). {quick.reason}".rstrip()
            )
        lines.append(untrusted("pr.title", pr.title, 500))
        lines.append(untrusted("pr.body", pr.body or "(empty)", 6000))
        lines.append(
            untrusted("pr.files", "\n".join(f.path for f in pr.files[:200]), 6000)
        )
        lines.append(
            "When the maintainer says 'this PR' they mean this one; put its repo and "
            "number in any proposal."
        )
    elif context.repo:
        record = await service.store.get_repo(context.repo)
        evaluations = await service.store.list_evaluations(context.repo)
        lines.append(
            f"The maintainer is looking at the repository {context.repo}"
            + (
                f" ({'active' if record.active else 'pending approval'}"
                f"{', paused' if record.paused else ''})"
                if record
                else ""
            )
            + f", with {len(evaluations)} evaluated open PRs. 'This repo' means it."
        )
        layer = await service.a.memory.read_text(profile_path(context.repo))
        lines.append(
            f"Its settings layer (what kind=configure with repo={context.repo} replaces):\n"
            + (layer or "(empty)")[:8000]
        )
        active = await service.profiles.active(context.repo)
        if active is not None:
            setup = await review_setup(service, context.repo, active)
            use = setup["in_use"]
            lines.append(
                "How its PRs are reviewed: review.skill is "
                f"{setup['skill']!r}, so reviews follow "
                + (
                    f"the repository's skill {use['name']} ({use['path']})"
                    if use["kind"] == "repo"
                    else f"the service's skill {use['name']}"
                    if use["kind"] == "service"
                    else "a repository skill that is no longer there (the service's "
                    "skill instead)"
                )
                + ". Its skills: "
                + (
                    "; ".join(
                        f"{x['path']} ({x['name']})" for x in setup["repo_skills"]
                    )
                    or "none"
                )
                + ". To change it, set review: {skill: builtin:pr-review (the "
                "repository's review skill when it has one, else the service's) | "
                "service:<name> (the service's even so) | repo:<path of a SKILL.md "
                "above>, instructions: <how its maintainers want PRs reviewed, in their "
                "words>} in its settings layer. Instructions now: "
                + (repr(setup["instructions"]) if setup["instructions"] else "none")
                + "."
            )
        offered = [
            (name, m)
            for name, m in sorted(service.machines.items())
            if m.serves(context.repo)
        ]
        if offered:
            profile = await service.profiles.active(context.repo)
            lines.append(
                "Test machines offered to it. The service settings own the machines; "
                "this repository's layer decides whether it uses one and how, as "
                "labs: {<machine>: {use: true, setup: <shell run before each "
                "reproduction, such as activating its environment>, timeout_seconds, "
                "guard: {forbid: [<regex>]}, protected_paths: [...]}}:"
            )
            for name, m in offered:
                binding = profile.labs.get(name) if profile else None
                lines.append(
                    f"- {name}: {m.description or 'no description'}; "
                    + ("used" if binding and binding.use else "not used")
                    + (
                        f", setup {binding.setup!r}"
                        if binding and binding.setup
                        else ""
                    )
                )
    elif context.page == "settings":
        lines.append(
            "The maintainer is on the Settings page: requests here are about the "
            "service settings below unless they name a repository."
        )
    return "\n".join(lines)


async def _settings_block(service: Any, user: User) -> str:
    """The service settings, their defaults, and how to change them, for the agent.

    Secret values are never included: only which secret names are set.
    """
    text, legacy = await service.settings_text()
    config = service.service_config
    agents = effective_agents(config, legacy)
    host_agent = service.host_values["default_agent"]
    routes = effective_routing(service.default_routing, config, host_agent)
    default_agent = config.default_agent or host_agent
    names = secret_names(config)
    infos = await service.a.secrets.describe(names) if service.a.secrets else []
    decision = await service.service_gate(user.login)
    runtimes = getattr(service.a.agents, "runtimes", {}) or {}

    def offered(name: str) -> str:
        found = runtimes.get(name)
        cached = getattr(found, "cached_choices", None)
        choices = cached() if callable(cached) else None
        if choices is None:
            return ""
        models = ", ".join(m.id for m in choices.models) or "any it accepts"
        levels = ", ".join(choices.efforts) or "none"
        return (
            f"; models it offers: {models} (its default: {choices.default_model or 'unknown'}); "
            f"reasoning levels: {levels} (its default: {choices.default_effort or 'unknown'})"
        )

    agent_lines = [
        f"- {name}: {a.adapter}, {'enabled' if a.enabled else 'disabled'}, "
        f"{'installed' if service.agent_ready(name) else 'NOT installed on this host'}, "
        f"model {a.model or 'CLI default'}, effort {a.effort or 'route or CLI default'}, "
        f"provider {a.provider or 'its own sign-in'}, "
        f"permissions {a.permissions or 'per task route'}, "
        + (
            f"confined by command_prefix {a.command_prefix}"
            if a.command_prefix
            else "unconfined (runs as the service's user and can read its files)"
        )
        + (
            f", approves without asking: {a.approve_commands}"
            if a.approve_commands
            else ""
        )
        + "; custom providers it can use: "
        f"{', '.join(sorted(PROVIDER_PROTOCOLS.get(a.adapter, ()))) or 'none'}"
        + offered(name)
        for name, a in agents.items()
    ]
    route_lines = [
        f"- {task}: runs on {service.runs_on(r)}; order {r.primary}"
        + (f" then {', '.join(r.fallback)}" if r.fallback else "")
        + (f", model {r.model}" if r.model else "")
        + f", timeout {r.timeout_seconds}s, permissions {r.permissions}"
        for task, r in routes.items()
    ]
    secrets = ", ".join(
        f"{i.name} ({'set' if i.configured else 'not set'})" for i in infos
    )
    return "\n".join(
        [
            "Service settings (config/service.yaml in the memory repo). Only the owner of "
            f"this service ({service.settings.owner or 'any admin of ' + (service.settings_repo() or 'its repository') + ', until one takes ownership'}) "
            f"may apply them; {user.login} {'may' if decision.allowed else 'may not, so any change waits for the owner'}.",
            "Current document:",
            text.strip() or "(empty: every default applies)",
            "Agents as they run now:",
            *agent_lines,
            "Default agent: "
            + (
                f"{default_agent} (from {'the settings' if config.default_agent else 'setup'})"
                if default_agent
                else "none; each task follows its route's own order"
            ),
            "Default routes per task (a repository's own routing still overrides them):",
            *route_lines,
            f"Secrets the providers name: {secrets or 'none'}.",
            "Shape of the document; every section is optional and lists only what "
            "differs from the defaults:\n"
            "schema: 1\n"
            "default_agent: <agent name> (every task tries it first, except verify, which "
            "prefers a second agent; routes below still win)\n"
            "agents: {<name>: {enabled, model, effort (one of its reasoning levels), provider, "
            "budget: {daily_usd, daily_runs}, "
            "permissions: auto | restricted | bypass (its mode for every task instead of "
            "the route's; bypass skips the CLI's permission checks on team PRs only, "
            "unless a route sets bypass_untrusted; omit to follow each route), "
            "command_prefix: [<argv of a wrapper the CLI runs under, such as sudo -u "
            "<agent-user> -- or bwrap ... --; {workdir} and {run_dir} are filled in per "
            "run>] (keeps the agent from reading the host's files, such as the tokens "
            "reviewers signed in with), "
            "approve_commands: [<a command's leading words that the service approves for "
            "this agent without asking, on any PR, such as python3 .agents/skills/docs-check/"
            "check_rst_markup.py; only commands that cannot run a PR's code or reach outside "
            "its checkout: a repository's own read-only scripts, never an interpreter or "
            "shell without its script, a test or build runner, or the network>]}}\n"
            "providers: {<id>: {name, protocol: anthropic | openai | openai-responses, "
            "base_url, api_key: <SECRET_NAME>, models: [...]}}\n"
            "routing: {<task>: {primary, fallback: [...], verifier, model, effort, "
            "timeout_seconds, permissions: auto | restricted}}\n"
            "system: {poll_seconds, max_public_writes_per_hour, max_repo_public_writes_per_hour}\n"
            "labs: {<name>: {description: <its environment and rules in plain language: hardware, "
            "how to set up the environment, where data lives, what runs must not do, how long "
            "they may take>, transport: ssh | local, host: <HostName: an address, name, or SSH "
            "alias>, user, port, proxy_jump: <[user@]host[:port], comma-separated>, "
            "identity_file: <a private key path on the service's host>, repos: [owner/name, ...] "
            "(empty: every repository), and what the service enforces, drafted from the "
            "description: workdir, setup: <shell run before each reproduction>, forbid: [<regex>], "
            "timeout_seconds, max_gpu_memory_mib, max_gpu_util_pct}} — test machines where a PR's "
            "code runs to reproduce what it claims; the service user's SSH keys must reach the "
            "host, and the Settings page drafts the enforced fields from the description",
            "To change settings, propose kind=configure with the complete new document in "
            "settings_yaml and a one-line summary; the maintainer sees the diff and applies "
            "it. A provider names its key by a secret name such as DEEPSEEK_API_KEY. Never "
            "ask for, repeat, or write a key's value: tell the maintainer to paste it into "
            "the key field on the Settings page.",
        ]
    )


async def _configure(
    service: Any, user: User, p: ProposedAction, repos: list[str]
) -> tuple[str | None, str | None]:
    """Draft the settings change the agent proposed; the maintainer applies it.

    Returns the memory change id, or a sentence saying why there is none.
    """
    doc = p.settings_yaml.strip()
    if not doc:
        return None, None
    _, found = redact_secrets(doc)
    if found:
        return None, (
            "The drafted settings contained what looks like a credential, so I did not "
            "keep them. Keys go in the key field on the Settings page."
        )
    title = (p.summary or "settings: update").strip()[:120]
    if not p.repo:
        try:
            change, _ = await service.propose_settings(
                doc, user.login, title, approve=False
            )
        except SettingsError as e:
            return None, "That settings change does not check: " + "; ".join(e.problems)
        return change.id, None
    if p.repo not in repos:
        return None, f"You cannot see {p.repo}, so I cannot change its settings."
    try:
        layer = parse_yaml(doc, "settings")
        _, change = await service.profiles.propose_memory_layer(
            p.repo, layer, user.login, title
        )
    except ProfileError as e:
        return None, f"That change to {p.repo}'s settings does not check: {e}"
    change.requested_by = user.login
    await service.store.save_memory_change(change)
    await service.submit_memory_change(change)
    return change.id, None


ACTIVE = [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING]


async def _guide(service: Any) -> str:
    """The service guide the chat agent follows (``skills/maintainer-chat``): what
    the service is, where each feature lives, and what each proposal does."""
    path = Path(service.settings.skills_dir) / "maintainer-chat" / "SKILL.md"
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except OSError:
        return ""
    return "Service guide:\n" + re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


async def _may_decide(service: Any, user: User, req: ChatRequest) -> tuple[bool, str]:
    """Whether ``user`` may approve ``req``, and why not: an ``improve`` needs the
    right to approve changes to the service's code, an ``add_repo`` the right to
    configure that repository."""
    if req.kind == "improve":
        return await code_maintainer(service, user)
    try:
        repo = check_repo(req.text)
    except ValueError as e:
        return False, str(e)
    gate = await service.gate.check(
        repo, user.login, await service.profiles.active(repo), person=True
    )
    return gate.allowed, gate.reason


async def _running_improvement(service: Any, thread_id: str) -> str | None:
    """The job id of this conversation's self-improvement still queued or running."""
    for job in await service.store.list_jobs(ACTIVE, limit=1000):
        if job.playbook == "self_improve" and job.key.startswith(
            f"self_improve:{thread_id}:"
        ):
            first = job.events[0].get("id") if job.events else None
            return first or job.id
    return None


async def _improve_request(
    service: Any, p: ProposedAction, thread_id: str
) -> tuple[str | None, str | None]:
    """Check an improve proposal before it becomes a request card: returns a running
    job to link instead (a conversation runs one change at a time), or a note when it
    cannot be done here."""
    if not p.request.strip():
        return None, "The request was empty."
    if not service.settings.code_repo:
        return None, (
            "This service has no code repository configured, so it cannot change "
            "its own code."
        )
    running = await _running_improvement(service, thread_id)
    if running:
        return running, (
            "A change to the service's code from this conversation is still "
            "running, so I did not ask for another one; it is linked below."
        )
    for message in await service.store.list_messages(thread_id):
        if any(r.kind == "improve" and r.state == "pending" for r in message.requests):
            return None, (
                "A change to the service's code from this conversation is still "
                "waiting for approval above; approve or decline that one first."
            )
    return None, None


async def _start_improve(
    service: Any,
    asked_by: str,
    approved_by: str,
    request: str,
    thread_id: str,
    history: list[Any],
) -> str:
    """Start the ``self_improve`` playbook for an approved request; returns the id
    the jobs API opens it by. Its context is what people wrote in the conversation,
    never the agent's answers, which can repeat text from PRs: the work runs on the
    service's own code as trusted input."""
    context = "\n".join(
        f"{m.author or asked_by}: {m.text}" for m in history[-10:] if m.role == "user"
    )
    event = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        payload={
            "command": "improve",
            "request": request,
            "by": asked_by,
            "approved_by": approved_by,
            "thread_id": thread_id,
            "context": context[-8000:],
            "request_id": f"{thread_id}:{service.clock.now().timestamp()}",
        },
        source="chat",
    )
    await service.bus.publish(event)
    return event.id


async def _governance(service: Any, user: User, p: ProposedAction) -> str:
    """Start the agent on ``p.repo``'s People record; returns the request event's id,
    which the jobs API resolves to the job. What it drafts waits for a maintainer."""
    instruction, _ = redact_secrets(p.request.strip())
    event = Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=p.repo,
        payload={
            "command": "governance",
            "by": user.login,
            "mode": p.people_mode,
            "instruction": instruction,
        },
        source="chat",
    )
    await service.bus.publish(event)
    return event.id


async def _evaluate(service: Any, user: User, p: ProposedAction) -> str:
    """Score ``p.number``'s priority again, or every open PR's in ``p.repo`` when it
    has no number, with the same limits as the dashboard; returns a note for the
    answer."""
    numbers = (
        [p.number]
        if p.number
        else [
            s.number for s in await service.store.list_snapshots(p.repo, open_only=True)
        ]
    )
    out = await start_evaluations(
        service, user.login, [(p.repo, n) for n in numbers], "chat"
    )
    started = sum(1 for o in out if o["run"] == "evaluate")
    joined = sum(1 for o in out if o["run"] == "joined")
    if not started and not joined:
        return f"Priority scoring is off for {p.repo}, so nothing was scored."
    parts = [f"Scoring the priority of {started} PR(s) in {p.repo} again"]
    if joined:
        parts.append(f"{joined} already being scored again")
    if len(numbers) > len(out):
        parts.append(f"the first {len(out)} of {len(numbers)}")
    return (
        "; ".join(parts)
        + ". The new tiers appear on the Pull requests page as each finishes."
    )


async def save_lesson(service: Any, user: User, p: ProposedAction) -> str:
    """Save a review lesson for ``p.repo`` as a memory change and return its id. It
    carries ``user``'s approval when they may configure the repository, so it merges
    at once; otherwise it waits for a maintainer who may."""
    path = f"{repo_dir(p.repo)}/knowledge/review_lessons.md"
    current = await service.a.memory.read_text(path) or "# Review lessons\n"
    lesson = p.lesson.strip()
    item = "\n  ".join(line.rstrip() for line in lesson.splitlines())
    change = MemoryChange(
        tier=Tier.T1,
        title=f"lesson from {user.login}",
        description=lesson,
        files={path: current.rstrip() + f"\n- {item}\n"},
        repos=[p.repo],
        requested_by=user.login,
        created_at=service.clock.now(),
    )
    decision = await service.gate.check(
        p.repo, user.login, await service.profiles.active(p.repo), person=True
    )
    if decision.allowed:
        change.approvals = {p.repo: [user.login]}
    await service.store.save_memory_change(change)
    await service.store.add_signal(
        Signal(
            kind=SignalKind.CHAT_INSTRUCTION,
            repo=p.repo,
            source=user.login,
            payload={"lesson": p.lesson},
        )
    )
    await service.pipeline.submit(
        make_action(
            ActionKind.MEMORY_CHANGE,
            p.repo,
            MemoryChangePayload(change_id=change.id),
            discriminator=change.id,
        )
    )
    return change.id


async def _propose(service: Any, user: User, p: ProposedAction) -> list[str]:
    """Turn a chat proposal into actions that wait for the maintainer's approval."""
    if p.kind == "review" and p.number:
        await service.bus.publish(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo=p.repo,
                number=p.number,
                payload={"command": "review", "by": user.login},
                source="chat",
            )
        )
        return []
    pr = await service.a.forge.get_pr(p.repo, p.number) if p.number else None
    if pr is None:
        return []
    disc = f"chat:{service.clock.now().timestamp()}"
    if p.kind == "label":
        action = make_action(
            ActionKind.LABEL_APPLY,
            p.repo,
            LabelPayload(labels=p.labels),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "rerun" and p.run_id:
        action = make_action(
            ActionKind.CI_RERUN_FAILED,
            p.repo,
            RerunPayload(run_id=p.run_id),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "request_reviewers":
        action = make_action(
            ActionKind.PR_REQUEST_REVIEWERS,
            p.repo,
            RequestReviewersPayload(logins=p.logins),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "comment" and p.body:
        action = make_action(
            ActionKind.COMMENT_UPSERT,
            p.repo,
            CommentUpsertPayload(purpose=f"chat-{disc}", body=p.body),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    elif p.kind == "nudge" and p.body:
        action = make_action(
            ActionKind.PR_NUDGE,
            p.repo,
            NudgePayload(body=p.body),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=disc,
        )
    else:
        return []
    action.provenance.requested_by = user.login
    action.rationale = f"requested by {user.login} in chat"
    record = await service.pipeline.submit(action, force_propose=True)
    proposal = await service.store.proposal_for_action(record.action.id)
    return [proposal.id] if proposal else []
