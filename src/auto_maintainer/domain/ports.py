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

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    InstalledAgent,
    PersonalAgent,
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
    PersonAlias,
    PrAssignee,
    ProfileState,
    RepoRecord,
    ReviewRecord,
)
from .scoring import Evaluation
from .settings import LabMachine, Provider, SecretInfo, UserToken
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

    async def in_merge_queue(self, repo: str, number: int) -> bool | None:
        """Whether the PR waits in its base branch's merge queue; ``None`` when the
        forge has none or does not say."""
        ...

    async def get_pr_diff(self, repo: str, number: int) -> str | None:
        """The PR's unified diff against its base at its current head, or ``None``."""
        ...

    async def get_permission(self, repo: str, login: str) -> Permission: ...

    async def is_team_member(self, repo: str, team: str, login: str) -> bool:
        """Whether ``login`` belongs to ``team`` on ``repo``'s forge, as CODEOWNERS
        names it without the ``@``: ``org/team`` on GitHub, a group path on GitLab."""
        ...

    async def path_changed_by(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Login of whoever landed the latest commit on ``ref`` that touched ``path``.

        For a commit that came from a merged PR this is the user who merged the PR;
        otherwise the commit's pusher or committer. ``None`` when unknown.
        """
        ...

    async def default_branch_sha(self, repo: str) -> str: ...

    async def behind_by(self, repo: str, base: str, head: str) -> int | None:
        """How many commits of branch ``base`` the commit ``head`` lacks; ``None``
        when the forge cannot compare them."""
        ...


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
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> None: ...

    async def delete_comment(self, repo: str, number: int, comment_id: int) -> None: ...

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

    async def approve_workflow_run(self, repo: str, run_id: int) -> None:
        """Approve a workflow run GitHub holds for a maintainer, so it runs."""
        ...

    async def approve_pr(
        self, repo: str, number: int, commit_sha: str, body: PublicText
    ) -> CommentRef:
        """Approve the PR at ``commit_sha``. Only a writer acting as a person does;
        the service's own writer raises ``PermissionError``."""
        ...

    async def reply_to_review_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> CommentRef: ...

    async def resolve_review_thread(
        self, repo: str, number: int, thread_id: str
    ) -> None:
        """Mark review thread ``thread_id`` (the id the reader keeps for it)
        resolved."""
        ...

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

    async def enqueue_merge(self, repo: str, number: int, sha: str) -> None:
        """Add the PR at ``sha`` to its base branch's merge queue; forges without
        one raise ``NotImplementedError``."""
        ...

    async def update_pr(
        self, repo: str, number: int, title: PublicText, body: PublicText
    ) -> None:
        """Set the PR's title and description."""
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

    async def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: PublicText,
        body: PublicText,
        *,
        draft: bool = False,
    ) -> CommentRef:
        """Open a PR on a repository the service maintains (``head`` may be
        ``owner:branch`` from another repository in the same fork network)."""
        ...

    async def merge_service_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> str: ...


@runtime_checkable
class CiProvider(Protocol):
    async def get_job_log(self, repo: str, job_id: int) -> str | None: ...

    async def get_status_log(self, repo: str, url: str) -> str | None:
        """The log behind a commit status an outside service reported (its link),
        for the services the adapter knows; ``None`` for any other."""
        ...

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


ProgressObserver = Callable[[AgentTask, str, AgentEvent], Awaitable[None]]
"""Called with the task, the runtime's name, and each event of every attempt."""


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

    def bind_progress(self, observer: ProgressObserver) -> None:
        """Report every attempt's events to ``observer``, credentials scrubbed. An
        observer that raises never stops the run."""
        ...

    def tell(self, job_id: str, by: str, text: str) -> bool:
        """Bring ``text``, a message from the reviewer ``by``, to the agent running for
        ``job_id`` now (a task with ``messages`` on a CLI that can take one). Whether
        an agent took it; a message nobody takes is not kept."""
        ...

    def listening(self, job_id: str) -> bool:
        """Whether an agent running for ``job_id`` takes messages now."""
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

    async def changes(self, path: Any) -> WorkChanges:
        """Everything changed in the worktree since its checkout, as one patch."""
        ...

    async def apply(self, path: Any, patch: str) -> None:
        """Apply ``patch`` (as ``changes`` returns it) to the worktree, so an agent
        goes on from a change made earlier; raises when it does not apply."""
        ...

    async def merge_base(self, path: Any, a: str, b: str) -> str | None:
        """The commit where ``b`` branched from ``a``, or ``None``."""
        ...

    async def merge(self, path: Any, branch: str) -> MergeAttempt:
        """Start merging ``branch`` of the base repository into the worktree's commit,
        without committing; the files that conflict keep their conflict markers."""
        ...

    async def archive(self, path: Any, ref: str, max_bytes: int = 200_000_000) -> str:
        """The files of ``ref`` as a gzipped tar, base64-encoded, for copying the code
        to a test machine without giving it any credentials.

        Raises:
            ValueError: The archive is larger than ``max_bytes``.
        """
        ...


class ConflictSides(BaseModel):
    """A conflicted file on each side of a merge: where the two parted (``base``),
    the PR's (``ours``), and the merged branch's (``theirs``); ``None`` for a side
    without the file, or a file too large or not text."""

    base: str | None = None
    ours: str | None = None
    theirs: str | None = None


class MergeAttempt(BaseModel):
    merge_sha: str
    """The commit of the merged branch."""
    conflicts: list[str] = Field(default_factory=list)
    sides: dict[str, ConflictSides] = Field(default_factory=dict)
    """Each conflicted file on each side, as git records them before resolving."""
    steering: list[str] = Field(default_factory=list)
    """The conflicted files that steer coding agents (``AGENTS.md``, skills). The
    checkout holds the default branch's version of them, so no agent reads a PR's own
    instructions, and none resolves them: a person does."""


class WorkChanges(BaseModel):
    base_sha: str
    patch: str = ""
    files: list[str] = Field(default_factory=list)


@runtime_checkable
class BranchPublisher(Protocol):
    """Puts a patch on a new branch of the service's own repository (actuator only)."""

    async def publish(
        self,
        repo: str,
        base_sha: str,
        patch: str,
        branch: str,
        message: str,
        author: tuple[str, str] | None = None,
        *,
        merge_sha: str | None = None,
        resolved: dict[str, str] | None = None,
        push_to: str | None = None,
    ) -> str:
        """Apply ``patch`` on ``base_sha`` (or, with ``merge_sha``, merge that commit
        into ``base_sha``, taking ``resolved`` for the files that conflict), commit it
        (as ``author``: name and email, else the service), and push ``branch`` of
        ``push_to`` (else ``repo``) without forcing; return the commit."""
        ...


class CommitInfo(BaseModel):
    sha: str
    author: str = ""
    subject: str = ""


class UpdateStatus(BaseModel):
    """Which code the service runs and what its repository's default branch has."""

    running_sha: str
    branch: str = ""
    upstream_sha: str | None = None
    pending: list[CommitInfo] = Field(default_factory=list)
    """Commits on the default branch that the service does not run yet."""
    dirty: bool = False
    """The service's checkout has local changes, so it cannot be updated."""
    error: str | None = None


class DeployResult(BaseModel):
    ok: bool
    sha: str
    previous_sha: str = ""
    log: str = ""


@runtime_checkable
class ServiceUpdater(Protocol):
    """Updates the code the service runs (design 5.12)."""

    async def status(self) -> UpdateStatus: ...

    async def confirm(self) -> dict[str, Any] | None:
        """Mark the running deploy healthy so it is not rolled back; return its record
        (``previous_sha``, ``sha``, ``rolled_back``) the first time, else ``None``."""
        ...

    async def deploy(self, sha: str) -> DeployResult:
        """Test ``sha`` in a staging worktree, then switch the service's checkout to it.

        The caller restarts the service after a successful deploy.
        """
        ...


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
    """Runs a repository's own check scripts with no credentials and a timeout.
    ``untrusted``: the code it runs is an agent's work (the tests of a change the
    service made to itself), so it runs where agents do, in a container of its own,
    when the host has one."""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        untrusted: bool = False,
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
        stdin: str | None = None,
        on_output: Callable[[str], Awaitable[None]] | None = None,
    ) -> CommandResult:
        """Run ``command``; with ``on_output``, each line of its output is handed to
        it as it comes."""
        ...

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
    # When each entry of ``permissions`` was read from the forge; each expires alone.
    permissions_checked: dict[str, datetime] = Field(default_factory=dict)


class SecretStore(Protocol):
    """Values of the secrets service settings name, such as providers' API keys.

    They stay on the service host. A value set from the dashboard wins over one in the
    host environment (``secrets.env``). Nothing outside the agents' environment ever
    receives a value: the dashboard sees only ``SecretInfo``.
    """

    async def get(self, name: str) -> str | None: ...
    async def describe(self, names: list[str]) -> list[SecretInfo]: ...
    async def set(self, name: str, value: str, by: str) -> None: ...
    async def delete(self, name: str) -> None: ...


class AgentInstaller(Protocol):
    """Installs agent CLIs on the service's host (``agents/install.py``)."""

    def sources(self) -> dict[str, str]:
        """The CLIs this host can install, by runtime name, with where each comes from."""
        ...

    async def install(
        self, name: str, on_line: Callable[[str], Awaitable[None]] | None = None
    ) -> InstalledAgent: ...

    async def latest(self, name: str) -> str:
        """The latest version of ``name`` its publisher offers."""
        ...


class PersonalAgents(Protocol):
    """Each person's own agent accounts on the host (``store/people.py``): the home
    folder their CLIs sign in to, how they run each agent, their own model APIs, and
    those APIs' keys, which only runs of their own tasks read."""

    def home(self, login: str) -> Path: ...
    async def get(self, login: str) -> dict[str, PersonalAgent]: ...
    async def put(self, login: str, setup: PersonalAgent) -> None: ...
    async def providers(self, login: str) -> dict[str, Provider]: ...
    async def put_provider(
        self, login: str, provider_id: str, provider: Provider | None
    ) -> None: ...
    async def provider_key(self, login: str, provider_id: str) -> str | None: ...
    async def set_provider_key(
        self, login: str, provider_id: str, value: str | None
    ) -> None: ...
    async def keyed(self, login: str) -> set[str]: ...
    async def sign_out(self, login: str, paths: tuple[str, ...]) -> None: ...
    async def ssh_public_key(self, login: str) -> str: ...
    async def machines(self, login: str) -> dict[str, LabMachine]: ...
    async def put_machine(
        self, login: str, name: str, machine: LabMachine | None
    ) -> None: ...
    async def all_machines(self) -> dict[str, LabMachine]: ...


class ScopeGrant(Protocol):
    """Adding scopes to the service's forge sign-in: a device flow its account
    approves on the forge (``forges/github/auth.py:ScopeRequest``)."""

    code: str | None
    url: str | None

    async def start(self, wait: float = 30) -> dict[str, str]:
        """Start it; the one-time code and the page to enter it on."""
        ...

    async def wait(self) -> bool:
        """Whether the account approved, once it ends."""
        ...

    def state(self) -> str:
        """``pending``, ``done``, or ``failed``."""
        ...

    def cancel(self) -> None: ...


class UserTokens(Protocol):
    """Signed-in maintainers' own GitHub tokens, on the host only (``UserToken``)."""

    async def get(self, login: str) -> UserToken | None: ...
    async def set(self, login: str, token: UserToken) -> None: ...
    async def delete(self, login: str) -> None: ...


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
        self,
        pool: str,
        limit: int,
        lease_seconds: int,
        now: datetime,
        *,
        asked: bool | None = None,
    ) -> list[Job]:
        """Mark up to ``limit`` queued jobs of ``pool`` running and return them: the
        ones a person asked for first (``Job.asked_by``), each kind oldest first.
        ``asked`` limits them to people's requests (``True``) or to the service's own
        work (``False``)."""
        ...

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
        self,
        limit: int = 100,
        runtime: str | None = None,
        *,
        repo: str | None = None,
        since: datetime | None = None,
    ) -> list[AgentRunRecord]:
        """Agent runs, newest first."""
        ...

    async def agent_usage_since(
        self, runtime: str, since: datetime
    ) -> tuple[int, float]:
        """Runs and dollars of ``runtime`` on the service's own accounts since
        ``since``, for its budget; runs on a person's own account do not count."""
        ...

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
    async def revise_review_record(
        self, repo: str, number: int, action_id: str, result: dict[str, Any]
    ) -> bool:
        """Replace the findings of the review ``action_id`` posted, once its posted text
        was edited; whether there was one."""
        ...

    async def list_review_records(
        self, repo: str, number: int
    ) -> list[ReviewRecord]: ...
    async def add_ci_observation(self, obs: CiObservation) -> None: ...
    async def set_ci_verdict(
        self, obs_id: str, verdict: str | None, by: str | None, at: datetime | None
    ) -> CiObservation | None:
        """Record a person's verdict on a CI observation; ``None`` when there is no
        such observation."""
        ...

    async def list_ci_observations(
        self,
        repo: str | None = None,
        sha: str | None = None,
        since: datetime | None = None,
    ) -> list[CiObservation]: ...

    # merge arms, assignees, leases
    async def save_arm(self, arm: MergeArm) -> None: ...
    async def get_arm(self, repo: str, number: int) -> MergeArm | None: ...
    async def delete_arm(self, repo: str, number: int) -> None: ...
    async def list_arms(self, repo: str) -> list[MergeArm]:
        """Every armed merge in ``repo``, by PR number."""
        ...

    async def add_assignee(self, assignee: PrAssignee) -> PrAssignee:
        """Give a person a role on a PR, or keep the one there: a claim never turns
        an assignment back into a claim. Returns what is stored."""
        ...

    async def remove_assignee(
        self, repo: str, number: int, login: str, role: str
    ) -> bool:
        """Take ``login`` off the PR in ``role``; whether they held it."""
        ...

    async def list_assignees(
        self, repo: str | None = None, number: int | None = None
    ) -> list[PrAssignee]:
        """The owners and reviewers of PRs, oldest first."""
        ...

    async def list_aliases(self) -> list[PersonAlias]:
        """Every login's display name."""
        ...

    async def set_alias(self, alias: PersonAlias) -> None:
        """Give ``alias.login`` its display name, replacing the one it has."""
        ...

    async def delete_alias(self, login: str) -> None: ...

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
    async def threads_with_workdir(self) -> list[tuple[ChatThread, datetime]]:
        """Every conversation that keeps a working folder (``ChatThread.workdir``),
        with when it was last active: its newest message, else when it began."""
        ...

    async def add_message(self, message: ChatMessage) -> None: ...
    async def save_message(self, message: ChatMessage) -> None:
        """Replace a stored message, by its id."""
        ...

    async def messages_with_pending_requests(self) -> list[ChatMessage]:
        """Messages in any conversation with a request card still waiting."""
        ...

    async def rename_repo(self, old: str, new: str) -> int:
        """Give every record of repository ``old`` the name ``new``; returns the rows
        changed."""
        ...

    async def list_messages(self, thread_id: str) -> list[ChatMessage]: ...

    async def list_said(
        self, limit: int = 20_000
    ) -> list[tuple[str, ChatThread | None, ChatMessage]]:
        """Messages people wrote in conversations, oldest first, the newest
        ``limit``: who wrote each (its author, else its thread's owner), its thread,
        and the message."""
        ...

    # users, audit, service state
    async def save_user(self, user: UserRecord) -> None: ...
    async def get_user(self, login: str) -> UserRecord | None: ...
    async def add_audit(self, entry: AuditEntry) -> None: ...
    async def list_audit(
        self, repo: str | None = None, limit: int = 200
    ) -> list[AuditEntry]: ...
    async def get_state(self, key: str) -> Any | None: ...
    async def set_state(self, key: str, value: Any) -> None: ...
