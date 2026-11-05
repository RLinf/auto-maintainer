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

"""Ports: the interfaces between the application and the outside world (design 4.4).

Adapters implement these protocols; ``app.py`` injects them. Playbooks receive
readers only. ``ForgeWriter``, ``CiProvider.rerun_failed_jobs``, lab execution,
and memory writes are handed to the actuator and nothing else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .actions import Proposal, ProposalState
from .agents import (
    AgentAnswer,
    AgentEvent,
    AgentQuestion,
    AgentResult,
    AgentTask,
    Capabilities,
    QuestionState,
    RuntimeHealth,
)
from .events import Event
from .memory import MemoryChange, MemoryChangeState, Signal
from .merge_gate import MergeArm
from .permissions import Permission
from .profile import Route
from .records import (
    ActionRecord,
    AgentRunRecord,
    AuditEntry,
    ChatMessage,
    ChatThread,
    CiObservation,
    Job,
    JobStatus,
    ProfileState,
    RepoRecord,
    ReviewRecord,
)
from .scoring import Evaluation
from .snapshot import (
    CommentRef,
    IssueSnapshot,
    MergedPrSummary,
    PrSnapshot,
    RepoInfo,
    RunnerInfo,
)
from .visibility import PublicText

# ---------------------------------------------------------------------------
# Forge
# ---------------------------------------------------------------------------


@runtime_checkable
class ForgeReader(Protocol):
    """Read access to repositories on a forge."""

    async def service_login(self) -> str:
        """The login the service acts as, such as ``my-app[bot]``."""
        ...

    async def get_repo(self, repo: str) -> RepoInfo: ...

    async def list_open_prs(self, repo: str) -> list[PrSnapshot]: ...

    async def get_pr(self, repo: str, number: int) -> PrSnapshot | None: ...

    async def list_open_issues(self, repo: str) -> list[IssueSnapshot]: ...

    async def list_merged_prs(
        self, repo: str, limit: int = 50
    ) -> list[MergedPrSummary]: ...

    async def get_file(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """File content at ``ref`` (the default branch when ``None``), or ``None``."""
        ...

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        """Entry names in a directory, or an empty list."""
        ...

    async def get_permission(self, repo: str, login: str) -> Permission: ...

    async def is_team_member(self, org: str, team_slug: str, login: str) -> bool: ...

    async def path_changed_by(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Login of whoever landed the latest commit on ``ref`` that touched ``path``.

        For a commit that came from a merged PR this is the user who merged the PR;
        otherwise the commit's pusher or committer. ``None`` when unknown.
        """
        ...

    async def default_branch_sha(self, repo: str) -> str: ...


@dataclass(frozen=True)
class ReviewCommentSpec:
    path: str
    line: int
    body: PublicText
    start_line: int | None = None
    side: Literal["LEFT", "RIGHT"] = "RIGHT"


@runtime_checkable
class ForgeWriter(Protocol):
    """Write access to target repositories. Public text arrives as ``PublicText``."""

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...

    async def remove_label(self, repo: str, number: int, label: str) -> None: ...

    async def create_comment(
        self, repo: str, number: int, body: PublicText
    ) -> CommentRef: ...

    async def update_comment(
        self, repo: str, comment_id: int, body: PublicText
    ) -> None: ...

    async def delete_comment(self, repo: str, comment_id: int) -> None: ...

    async def create_review(
        self,
        repo: str,
        number: int,
        commit_sha: str,
        body: PublicText,
        event: Literal["COMMENT", "REQUEST_CHANGES"],
        comments: list[ReviewCommentSpec],
    ) -> CommentRef: ...

    async def update_review(
        self, repo: str, number: int, review_id: int, body: PublicText
    ) -> None: ...

    async def reply_to_review_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> CommentRef: ...

    async def request_reviewers(
        self, repo: str, number: int, logins: list[str]
    ) -> None: ...

    async def update_branch(
        self, repo: str, number: int, expected_head_sha: str
    ) -> None: ...

    async def merge(
        self,
        repo: str,
        number: int,
        sha: str,
        method: str,
        subject: PublicText,
        body: PublicText,
    ) -> str:
        """Merge and return the merge commit SHA."""
        ...

    async def convert_to_draft(self, repo: str, number: int) -> None: ...

    async def close(self, repo: str, number: int) -> None: ...

    async def create_service_issue(
        self, repo: str, title: str, body: str
    ) -> CommentRef:
        """Open an issue on a repository the service owns (its code or memory repo)."""
        ...

    async def create_service_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> CommentRef:
        """Open a PR on a repository the service owns."""
        ...

    async def merge_service_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> str: ...


@runtime_checkable
class CiProvider(Protocol):
    async def get_job_log(self, repo: str, job_id: int) -> str | None: ...

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None: ...

    async def list_runners(self, repo: str) -> list[RunnerInfo]: ...


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentRuntime(Protocol):
    """One agent CLI behind a uniform interface."""

    name: str

    def capabilities(self) -> Capabilities: ...

    async def probe(self) -> RuntimeHealth: ...

    def run(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        """Stream events; the last one is ``final`` and carries the ``AgentResult``."""
        ...


class AgentQuestions(Protocol):
    """Where agents' permission prompts and questions go while they run (design 5.7).

    ``ask`` records the question, alerts the repository's reviewers, and waits until
    one answers or ``timeout_seconds`` pass; an unanswered question is denied.
    """

    async def ask(
        self, question: AgentQuestion, timeout_seconds: float
    ) -> AgentAnswer: ...


class AgentRunner(Protocol):
    """Routes tasks to runtimes with fallback, and validates structured output."""

    async def run(
        self, task: AgentTask, route: Route, output_model: type[BaseModel] | None = None
    ) -> AgentResult:
        """Run ``task`` on ``route``. When ``output_model`` is given, ``result.structured``
        has been validated against it, or ``result.ok`` is false."""
        ...

    async def health(self) -> list[RuntimeHealth]: ...

    def bind_questions(self, questions: AgentQuestions) -> None:
        """Send runtimes' permission prompts and questions to ``questions``."""
        ...


# ---------------------------------------------------------------------------
# Memory, labs, notifications, events
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryStore(Protocol):
    """The memory repo checkout. Reads are for everyone; writes are for the actuator."""

    async def read_text(self, path: str) -> str | None: ...

    async def list_files(self, prefix: str) -> list[str]: ...

    async def head(self) -> str: ...

    async def refresh(self) -> bool:
        """Fetch the remote default branch; return whether ``head`` changed."""
        ...

    async def commit_direct(
        self, files: dict[str, str | None], message: str, author: str
    ) -> str:
        """Commit straight to the default branch (T0 only) and return the SHA."""
        ...

    async def stage_change(self, change: MemoryChange) -> str:
        """Commit ``change`` on its own branch and return the branch name."""
        ...

    async def merge_change(self, change: MemoryChange) -> str:
        """Merge the change's branch into the default branch and return the SHA."""
        ...

    async def drop_change(self, change: MemoryChange) -> None: ...

    def remote_repo(self) -> str | None:
        """``owner/repo`` of the memory repo on the forge, when it has one."""
        ...


@runtime_checkable
class Workspace(Protocol):
    """Git worktrees of target repositories for agent runs (design 4.7)."""

    async def prepare(
        self, repo: str, number: int | None, sha: str, default_branch: str
    ) -> Any:
        """Check out ``sha`` in a fresh worktree from the service's mirror and return its path.

        Agent instruction files and skills are replaced with their default-branch
        versions, so a PR cannot change how it is reviewed.
        """
        ...

    async def diff(
        self, path: Any, base: str, head: str, max_bytes: int = 200_000
    ) -> str:
        """The three-dot diff ``base...head`` in the worktree, truncated to ``max_bytes``."""
        ...

    async def release(self, path: Any) -> None: ...


class CommandResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False


class GpuStatus(BaseModel):
    index: int
    memory_used_mib: int
    memory_total_mib: int = 0
    utilization_pct: int = 0
    processes: int = 0


@runtime_checkable
class Sandbox(Protocol):
    """Runs a repository's own check scripts with no credentials and a timeout."""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> CommandResult: ...


@runtime_checkable
class LabHost(Protocol):
    name: str

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
    ) -> CommandResult: ...

    async def gpu_status(self) -> list[GpuStatus]: ...


@runtime_checkable
class Notifier(Protocol):
    async def send(self, channel: str, text: str, event: str) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    async def publish(self, event: Event) -> None: ...


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class UserRecord(BaseModel):
    login: str
    name: str = ""
    avatar_url: str = ""
    permissions: dict[str, str] = Field(default_factory=dict)
    permissions_checked_at: datetime | None = None


class Store(Protocol):
    """Operational state (design 7.1)."""

    # repositories and profiles
    async def save_repo(self, record: RepoRecord) -> None: ...
    async def get_repo(self, repo: str) -> RepoRecord | None: ...
    async def list_repos(self) -> list[RepoRecord]: ...
    async def get_profile_state(self, repo: str) -> ProfileState | None: ...
    async def save_profile_state(self, state: ProfileState) -> None: ...

    # snapshots and evaluations
    async def get_snapshot(self, repo: str, number: int) -> PrSnapshot | None: ...
    async def save_snapshot(self, pr: PrSnapshot) -> None: ...
    async def list_snapshots(
        self, repo: str | None = None, open_only: bool = True
    ) -> list[PrSnapshot]: ...
    async def get_evaluation(self, repo: str, number: int) -> Evaluation | None: ...
    async def save_evaluation(self, evaluation: Evaluation) -> None: ...
    async def list_evaluations(self, repo: str | None = None) -> list[Evaluation]: ...

    # jobs
    async def upsert_job(self, job: Job) -> tuple[Job, bool]:
        """Insert ``job`` unless one with the same playbook and key exists.

        An existing queued, running, or waiting job gets ``job.events`` appended and
        is returned with ``False``. A finished one is returned unchanged with ``False``.
        """
        ...

    async def get_job(self, job_id: str) -> Job | None: ...
    async def save_job(self, job: Job) -> None: ...
    async def claim_jobs(
        self, pool: str, limit: int, lease_seconds: int, now: datetime
    ) -> list[Job]: ...
    async def list_jobs(
        self,
        statuses: list[JobStatus] | None = None,
        repo: str | None = None,
        limit: int = 200,
    ) -> list[Job]: ...
    async def requeue_expired(self, now: datetime) -> int: ...

    # agent runs
    async def add_agent_run(self, run: AgentRunRecord) -> None: ...
    async def list_agent_runs(
        self, limit: int = 100, runtime: str | None = None
    ) -> list[AgentRunRecord]: ...
    async def agent_usage_since(
        self, runtime: str, since: datetime
    ) -> tuple[int, float]: ...

    # agents' questions
    async def save_question(self, question: AgentQuestion) -> None: ...
    async def get_question(self, question_id: str) -> AgentQuestion | None: ...
    async def list_questions(
        self, state: QuestionState | None = None, repo: str | None = None
    ) -> list[AgentQuestion]: ...

    # actions and proposals
    async def add_action(self, record: ActionRecord) -> tuple[ActionRecord, bool]:
        """Insert unless the idempotency key exists; return the stored record and whether it is new."""
        ...

    async def get_action(self, action_id: str) -> ActionRecord | None: ...
    async def save_action(self, record: ActionRecord) -> None: ...
    async def list_actions(
        self,
        repo: str | None = None,
        number: int | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[ActionRecord]: ...
    async def count_actions(
        self,
        statuses: list[str],
        since: datetime,
        repo: str | None = None,
        public_only: bool = True,
    ) -> int: ...
    async def add_proposal(self, proposal: Proposal) -> None: ...
    async def get_proposal(self, proposal_id: str) -> Proposal | None: ...
    async def proposal_for_action(self, action_id: str) -> Proposal | None: ...
    async def save_proposal(self, proposal: Proposal) -> None: ...
    async def list_proposals(
        self, state: ProposalState | None = None, repo: str | None = None
    ) -> list[Proposal]: ...

    # learning
    async def add_signal(self, signal: Signal) -> None: ...
    async def list_signals(
        self, repo: str | None = None, unconsumed: bool = True
    ) -> list[Signal]: ...
    async def save_signal(self, signal: Signal) -> None: ...
    async def add_review_record(self, record: ReviewRecord) -> None: ...
    async def list_review_records(
        self, repo: str, number: int
    ) -> list[ReviewRecord]: ...
    async def add_ci_observation(self, obs: CiObservation) -> None: ...
    async def list_ci_observations(
        self,
        repo: str | None = None,
        sha: str | None = None,
        since: datetime | None = None,
    ) -> list[CiObservation]: ...

    # merge arms, claims, leases
    async def save_arm(self, arm: MergeArm) -> None: ...
    async def get_arm(self, repo: str, number: int) -> MergeArm | None: ...
    async def delete_arm(self, repo: str, number: int) -> None: ...
    async def set_claim(self, repo: str, number: int, login: str | None) -> None: ...
    async def get_claims(self, repo: str | None = None) -> dict[str, str]: ...
    async def acquire_lease(
        self, lab: str, resource: str, holder: str, ttl_seconds: int, now: datetime
    ) -> bool: ...
    async def release_lease(self, lab: str, resource: str, holder: str) -> None: ...

    # memory changes
    async def save_memory_change(self, change: MemoryChange) -> None: ...
    async def get_memory_change(self, change_id: str) -> MemoryChange | None: ...
    async def list_memory_changes(
        self, state: MemoryChangeState | None = None
    ) -> list[MemoryChange]: ...

    # chat
    async def save_thread(self, thread: ChatThread) -> None: ...
    async def get_thread(self, thread_id: str) -> ChatThread | None: ...
    async def list_threads(self, owner: str) -> list[ChatThread]: ...
    async def add_message(self, message: ChatMessage) -> None: ...
    async def list_messages(self, thread_id: str) -> list[ChatMessage]: ...

    # users, audit, service state
    async def save_user(self, user: UserRecord) -> None: ...
    async def get_user(self, login: str) -> UserRecord | None: ...
    async def add_audit(self, entry: AuditEntry) -> None: ...
    async def list_audit(
        self, repo: str | None = None, limit: int = 200
    ) -> list[AuditEntry]: ...
    async def get_state(self, key: str) -> Any | None: ...
    async def set_state(self, key: str, value: Any) -> None: ...
