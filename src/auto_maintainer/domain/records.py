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

"""Operational records the store keeps: jobs, repositories, audit entries, and so on."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .actions import Action, ActionStatus, Decision
from .clock import utc_now
from .events import EventKind
from .ids import new_id


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WaitSpec(BaseModel):
    """What wakes a waiting job: an event of one of ``kinds`` about its target, or the deadline."""

    kinds: list[EventKind]
    repo: str | None = None
    number: int | None = None
    action_ids: list[str] = Field(default_factory=list)
    deadline: datetime
    reason: str = ""


class Job(BaseModel):
    playbook: str
    key: str
    repo: str | None = None
    number: int | None = None
    pool: str = "io"
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    events: list[dict[str, Any]] = Field(default_factory=list)
    """The events that triggered or woke the job, newest last."""
    wait: WaitSpec | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    lease_until: datetime | None = None
    not_before: datetime | None = None
    id: str = Field(default_factory=lambda: new_id("job"))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def asked_by(self) -> str | None:
        """The person who asked for this job on the dashboard or in a conversation
        (an event's ``by``); ``None`` for work the service started on its own."""
        for event in self.events:
            by = (event.get("payload") or {}).get("by")
            if by:
                return str(by)
        return None


class RepoRecord(BaseModel):
    repo: str
    active: bool = False
    """Whether the repository has an approved profile lock and is being served."""
    paused: bool = False
    added_by: str = ""
    added_at: datetime = Field(default_factory=utc_now)
    last_poll_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    watched_files: dict[str, str] = Field(default_factory=dict)
    """Path to content hash, for drift detection."""


class ActionRecord(BaseModel):
    action: Action
    decision: Decision | None = None
    status: ActionStatus = ActionStatus.NEW
    external_ref: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    approver: str | None = None
    post_as: str | None = None
    """The maintainer whose own GitHub account posts this action, when they chose to
    post a review draft as themselves; ``None`` posts as the service."""
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEntry(BaseModel):
    kind: str
    """``decision``, ``execution``, ``proposal``, ``config``, ``memory``, ``pause``, ..."""
    repo: str | None = None
    number: int | None = None
    actor: str = "service"
    action_id: str | None = None
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: new_id("aud"))
    created_at: datetime = Field(default_factory=utc_now)


class AgentRunRecord(BaseModel):
    id: str
    job_id: str | None = None
    repo: str | None = None
    runtime: str
    task_kind: str
    activity: str | None = None
    """``AgentTask.activity``: what the person was doing, when the kind does not say."""
    ok: bool
    permissions: str = "restricted"
    error: str | None = None
    session: str | None = None
    input_tokens: int = 0
    """Every token the model read, including ``cached_input_tokens``."""
    cached_input_tokens: int = 0
    """The part read from the provider's prompt cache. Before 2026-11-11 it was not
    recorded, and runs over ACP (Antigravity, OpenCode, Gemini CLI) left cache reads
    out of ``input_tokens``."""
    output_tokens: int = 0
    cost_usd: float | None = None
    transcript_path: str | None = None
    duration_seconds: float = 0.0
    requested_by: str | None = None
    account: str | None = None
    """The person whose own agent account ran it; ``None`` for the service's."""
    """The person whose request the run worked for; None for the service's own work."""
    created_at: datetime = Field(default_factory=utc_now)


ALIAS_MAX = 40


def clean_alias(text: str) -> str:
    """``text`` as a display name: spaces collapsed, 1 to ``ALIAS_MAX`` characters,
    no control characters and no leading ``@``.

    Raises:
        ValueError: It is empty, too long, or not plain text.
    """
    alias = " ".join(text.split())
    if not alias:
        raise ValueError("a name needs at least one character")
    if len(alias) > ALIAS_MAX:
        raise ValueError(f"a name has at most {ALIAS_MAX} characters")
    if alias.startswith("@") or any(ord(c) < 32 or ord(c) == 127 for c in alias):
        raise ValueError("a name is plain text and does not start with @")
    return alias


class PersonAlias(BaseModel):
    """The name a forge login goes by on the dashboard, such as a person's real
    name: their own (``set_by`` is the login), or one the service's owner gave a
    login whose person has set none, which their own replaces. The dashboard shows it
    in place of the login, with the login on hover."""

    login: str
    alias: str
    set_by: str
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def own(self) -> bool:
        return self.set_by.lower() == self.login.lower()


PrRole = Literal["owner", "reviewer"]
"""What a person takes on for a PR here: its ``owner`` answers for the whole PR's
progress and may assign its reviewers; a ``reviewer`` reviews it. Neither is a review
GitHub requested (its code owners), which stays GitHub's."""


class PrAssignee(BaseModel):
    """A person with a role on a PR in the service (design 2.3): one who claimed it
    themselves, or one assigned it (owners by a maintainer of the repository,
    reviewers by a maintainer or the PR's owner). A PR has any number of each, one
    person may hold both, and the login need not have signed in to the dashboard."""

    repo: str
    number: int
    login: str
    role: PrRole = "reviewer"
    kind: Literal["claimed", "assigned", "forge"] = "claimed"
    """``forge``: someone asked them on the forge itself (a review request, or an
    assignee for an owner), which the poller copies (``engine/poller.py:sync_roles``)."""
    by: str
    """Who claimed or assigned it: the login itself for a claim; the forge's name
    (``GitHub``) for a role from the forge."""
    at: datetime = Field(default_factory=utc_now)


class ReviewRecord(BaseModel):
    """A review or follow-up the service posted, with its structured findings."""

    repo: str
    number: int
    sha: str
    kind: str
    action_id: str
    result: dict[str, Any]
    external_id: int | None = None
    target: str = "review"
    created_at: datetime = Field(default_factory=utc_now)


class Culprit(BaseModel):
    """A place in the code that causes a CI failure."""

    path: str
    line: int | None = None
    why: str = ""


class RootCause(BaseModel):
    """Why CI failed, past the first classification: what fails, the mechanism, where
    in the code, and the smallest fix (design 5.5). One cause may explain several jobs."""

    jobs: list[str] = Field(
        default_factory=list,
        description="Names of the failed jobs this cause explains.",
    )
    failing: str = Field(description="The test, build step, or command that fails.")
    origin: Literal["change", "base", "infra", "flaky", "unclear"] = Field(
        description="change: this PR's change causes it. base: it fails without this "
        "change too (the base branch is broken). infra: the runner or the CI service. "
        "flaky: nondeterminism unrelated to the change. unclear: the evidence does not "
        "settle it."
    )
    cause: str = Field(description="Why it fails: the mechanism, not the symptom.")
    culprits: list[Culprit] = Field(
        default_factory=list,
        description="Where in the code, most relevant first: for origin change, the "
        "changed files and lines.",
    )
    fix: str = Field(default="", description="The smallest change that makes it pass.")
    evidence: list[str] = Field(
        default_factory=list, description="Up to five log lines that show it."
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CiObservation(BaseModel):
    repo: str
    number: int | None = None
    sha: str
    run_id: int | None = None
    check_id: int
    check_name: str
    workflow: str | None = None
    category: str
    signature: str | None = None
    confidence: float = 0.0
    explanation: str = ""
    rerun: bool = False
    root_cause: RootCause | None = None
    """For a failure that is not a flake: what a look at the code found."""
    fingerprint: str | None = None
    """The failure's identity across runs (``playbooks.ci_doctor.fingerprint``), so a
    failure seen before is not diagnosed again."""
    verdict: Literal["flaky", "real"] | None = None
    """A person's word on the failure, which outweighs any agent's: the same failure
    is taken that way after (``ci_doctor.first_looks``), and the first look learns
    from the repository's verdicts."""
    verdict_by: str | None = None
    verdict_at: datetime | None = None
    id: str = Field(default_factory=lambda: new_id("ci"))
    created_at: datetime = Field(default_factory=utc_now)


class ChatThread(BaseModel):
    owner: str
    title: str = ""
    repo: str | None = None
    number: int | None = None
    """The PR the conversation started on, if any."""
    workdir: str | None = None
    """The checkout of ``repo`` (at the PR's head, or its default branch) the agent
    works in for the whole conversation, so its session can resume there."""
    session: str | None = None
    runtime: str | None = None
    agent: str | None = None
    """The agent chosen for this conversation, or ``None`` for automatic routing."""
    live_run: str | None = None
    """The run answering the newest message, while it runs; its steps are live."""
    job_id: str | None = None
    """For a conversation about a review: the review or follow-up job. It is shared by
    everyone who may see the repository, and it lives on that review, not in the
    owner's list of conversations."""
    topic: str | None = None
    """For a conversation shared on a repository's page, such as ``people`` for its
    People record: everyone who may see the repository shares it, and it lives on
    that page, not in the owner's list of conversations."""
    id: str = Field(default_factory=lambda: new_id("chat"))
    created_at: datetime = Field(default_factory=utc_now)


class ChatRequest(BaseModel):
    """Something an answer asks to do that waits for a person's approval on its card:
    a change to the service's own code (``improve``), approved by a maintainer of the
    service's repository, or onboarding a repository (``add_repo``), approved by one
    who may configure it."""

    kind: Literal["improve", "add_repo"]
    text: str
    """The request for ``improve``; the ``owner/name`` for ``add_repo``."""
    state: Literal["pending", "approved", "declined"] = "pending"
    decided_by: str | None = None
    job: str | None = None
    """For an approved ``improve``: the id the jobs API opens its job by."""
    id: str = Field(default_factory=lambda: new_id("req"))


class ChatMessage(BaseModel):
    thread_id: str
    role: str
    """``user``, ``assistant``, or ``tool``."""
    text: str
    author: str | None = None
    """Who wrote a ``user`` message, in a conversation several people share."""
    proposals: list[str] = Field(default_factory=list)
    """Actions proposed in this message (or, about a review, the draft it revised),
    by action id."""
    changes: list[str] = Field(default_factory=list)
    """Memory changes this message made (configuration drafts, lessons), by id."""
    jobs: list[str] = Field(default_factory=list)
    """Jobs this message started, by the id of the request event, which the jobs API
    resolves."""
    requests: list[ChatRequest] = Field(default_factory=list)
    """What this message asks to do once a person approves it."""
    run_id: str | None = None
    """For an answer: the agent run that wrote it, whose steps stay readable."""
    runtime: str | None = None
    """For an answer: the agent runtime that wrote it."""
    while_running: bool = False
    """A ``user`` message sent to the agent while it wrote the review; the review
    answers it."""
    id: str = Field(default_factory=lambda: new_id("msg"))
    created_at: datetime = Field(default_factory=utc_now)


class ProfileState(BaseModel):
    """The store's mirror of a repository's lock and pending candidate."""

    repo: str
    lock_hash: str | None = None
    candidate: dict[str, Any] | None = None
    candidate_hash: str | None = None
    candidate_layers: dict[str, str] = Field(default_factory=dict)
    candidate_error: str | None = None
    candidate_diff: str | None = None
    approvals: list[str] = Field(default_factory=list)
    doctor: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)
