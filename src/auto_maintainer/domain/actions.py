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

"""Actions, policy decisions, and proposals.

An ``Action`` is one intended outward effect. Playbooks, MCP propose tools, and the
dashboard create actions; the policy engine decides each one; the actuator runs the
ones it may run. Payload models below define what each action kind carries.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .clock import utc_now
from .ids import new_id
from .visibility import Visibility


class ActionKind(StrEnum):
    LABEL_APPLY = "label.apply"
    LABEL_CI_GATE = "label.ci_gate"
    REVIEW_POST = "review.post"
    REVIEW_UPDATE = "review.update"
    THREAD_REPLY = "thread.reply"
    COMMENT_UPSERT = "comment.upsert"
    CI_RERUN_FAILED = "ci.rerun_failed"
    PR_REQUEST_REVIEWERS = "pr.request_reviewers"
    PR_APPROVE = "pr.approve"
    PR_UPDATE_BRANCH = "pr.update_branch"
    PR_MERGE = "pr.merge"
    PR_NUDGE = "pr.nudge"
    PR_CONVERT_TO_DRAFT = "pr.convert_to_draft"
    PR_CLOSE = "pr.close"
    PR_FIX = "pr.fix"
    LAB_REPRODUCE = "lab.reproduce"
    LAB_REVIEW = "lab.review"
    """A review's session on a test machine the repository uses: the reviewing agent
    tests and reproduces the PR in it as it sees fit, while it reviews."""
    ISSUE_LABEL = "issue.label"
    ISSUE_COMMENT = "issue.comment"
    LAB_RUN = "lab.run"
    MEMORY_COMMIT = "memory.commit"
    MEMORY_CHANGE = "memory.change"
    CODE_PR = "code.pr"
    CODE_MERGE = "code.merge"
    SERVICE_DEPLOY = "service.deploy"
    NOTIFY = "notify"

    @property
    def touches_repository(self) -> bool:
        """Whether the action changes a target repository or runs its code."""
        return self not in _INTERNAL_KINDS

    @property
    def visibility(self) -> Visibility:
        return Visibility.INTERNAL if self in _INTERNAL_KINDS else Visibility.PUBLIC


_INTERNAL_KINDS = {
    ActionKind.MEMORY_COMMIT,
    ActionKind.MEMORY_CHANGE,
    ActionKind.CODE_PR,
    ActionKind.CODE_MERGE,
    ActionKind.SERVICE_DEPLOY,
    ActionKind.NOTIFY,
}

ON_BY_DEFAULT = frozenset({ActionKind.LAB_REVIEW})
"""The one kind that runs a repository's code and is on by default (``auto``): a
review's session on a test machine. It runs only on a machine the repository's
maintainers turned on for it (``labs.<machine>.use``), writes nothing to the
repository, and checks every command against the machine's rules; a repository
sets it lower, for example to its team's PRs, like any level."""


class Autonomy(StrEnum):
    """How far an action kind may go without a human, from least to most."""

    OFF = "off"
    SHADOW = "shadow"
    PROPOSE = "propose"
    AUTO = "auto"

    @property
    def rank(self) -> int:
        return ["off", "shadow", "propose", "auto"].index(self.value)

    def cap(self, ceiling: Autonomy | None) -> Autonomy:
        """Return the lower of this level and ``ceiling``."""
        if ceiling is None or self.rank <= ceiling.rank:
            return self
        return ceiling


class DecisionKind(StrEnum):
    EXECUTE = "execute"
    PROPOSE = "propose"
    SHADOW = "shadow"
    DENY = "deny"


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: DecisionKind
    rule: str
    """The rule that decided, for example ``policy.label.apply=auto``."""
    reason: str = ""


class ActionStatus(StrEnum):
    NEW = "new"
    SHADOWED = "shadowed"
    DENIED = "denied"
    PROPOSED = "proposed"
    QUEUED = "queued"
    INTENT = "intent"
    EXECUTED = "executed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    NOOP = "noop"


class Provenance(BaseModel):
    job_id: str | None = None
    playbook: str | None = None
    agent_runs: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    requested_by: str | None = None
    """The maintainer who asked for the action, for dashboard and chat actions."""


class ActionHints(BaseModel):
    """Facts about the target that policy conditions read (design 5.8)."""

    author_is_team: bool | None = None
    recommendation: str | None = None
    """``ready``, ``changes``, or ``blocked`` when the action follows a review."""
    changed_paths: list[str] = Field(default_factory=list)


class Action(BaseModel):
    """One intended outward effect."""

    kind: ActionKind
    repo: str
    number: int | None = None
    head_sha: str | None = None
    """The head SHA the action was decided for; stale when the PR moved on."""
    payload: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    discriminator: str = ""
    """Distinguishes two actions of one kind on one target, such as two reruns."""
    provenance: Provenance = Field(default_factory=Provenance)
    hints: ActionHints = Field(default_factory=ActionHints)
    draft_for: str | None = None
    """The reviewer who asked for this draft. It becomes a proposal for them to edit and
    post, whatever the policy level, unless dev mode or a hard limit says otherwise."""
    id: str = Field(default_factory=lambda: new_id("act"))
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def visibility(self) -> Visibility:
        return self.kind.visibility

    @property
    def idempotency_key(self) -> str:
        target = f"#{self.number}" if self.number is not None else ""
        sha = (self.head_sha or "-")[:12]
        return f"{self.kind}:{self.repo}{target}@{sha}:{self.discriminator}"

    def typed(self) -> BaseModel:
        """Return the payload validated against this kind's payload model."""
        return PAYLOADS[self.kind].model_validate(self.payload)


class ProposalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class Proposal(BaseModel):
    """An action waiting for a human."""

    action_id: str
    repo: str
    state: ProposalState = ProposalState.PENDING
    id: str = Field(default_factory=lambda: new_id("prop"))
    created_at: datetime = Field(default_factory=utc_now)
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None
    edited: bool = False
    warnings: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    requested_by: str | None = None
    """For a requested review draft: the reviewer it is for."""


# ---------------------------------------------------------------------------
# Payloads, one model per action kind.
# ---------------------------------------------------------------------------


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabelPayload(_Payload):
    """Labels to add. Roles are resolved to label names by the actuator."""

    roles: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    remove_roles: list[str] = Field(default_factory=list)


class InlineComment(_Payload):
    path: str
    line: int
    start_line: int | None = None
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    body: str


class ReviewPostPayload(_Payload):
    body: str
    event: Literal["COMMENT", "REQUEST_CHANGES"] = "COMMENT"
    comments: list[InlineComment] = Field(default_factory=list)
    recommendation: str | None = None
    runtime: str | None = None
    kind: Literal["review", "followup"] = "review"
    result: dict[str, Any] | None = None
    """The structured review result, kept for follow-ups; never posted."""


class ReviewUpdatePayload(_Payload):
    """New text for a review or comment the service already posted. The action's
    ``head_sha`` is the commit that review covers, which its marker keeps."""

    target: Literal["review", "comment"]
    target_id: int
    body: str
    runtime: str | None = None
    kind: Literal["review", "followup"] = "followup"
    """The marker the new text keeps: the kind of review it replaces."""
    revises: str | None = None
    """The ``review.post`` action that posted the review; its body and findings
    change with the posted text, so follow-ups and scores read the revision."""
    result: dict[str, Any] | None = None
    """The revised structured review result, for ``revises``; never posted."""
    author: str | None = None
    """Who posted the review: the maintainer's login when they posted it as
    themselves, since only they can edit it, or ``None`` for the service."""


class ThreadReplyPayload(_Payload):
    comment_id: int
    body: str


class CommentUpsertPayload(_Payload):
    purpose: str
    """One comment per purpose and PR, such as ``hygiene`` or ``ci``."""
    body: str
    runtime: str | None = None


class RerunPayload(_Payload):
    run_id: int
    workflow: str | None = None
    attempt: int | None = None


class RequestReviewersPayload(_Payload):
    logins: list[str]


class UpdateBranchPayload(_Payload):
    pass


class ApprovePrPayload(_Payload):
    """A person's approval of a PR at ``Action.head_sha``. It runs only with their own
    GitHub sign-in (``ActionRecord.post_as``): the service's account never approves."""

    body: str = Field(default="", max_length=20_000)


class MergePayload(_Payload):
    method: Literal["squash", "merge", "rebase"] = "squash"
    subject: str
    body: str = ""
    armed_by: str | None = None


class NudgePayload(_Payload):
    body: str
    audience: Literal["reviewers", "author"] = "reviewers"


class PrStatePayload(_Payload):
    body: str = ""


class IssueLabelPayload(_Payload):
    labels: list[str]


class IssueCommentPayload(_Payload):
    body: str


class LabRunPayload(_Payload):
    lab: str
    template: str
    args: dict[str, str] = Field(default_factory=dict)
    cpu_only: bool = False


class MemoryCommitPayload(_Payload):
    path: str
    lines: list[str]
    message: str = "journal: record observations"


class MemoryChangePayload(_Payload):
    change_id: str
    replaces: str | None = None
    """An open memory change this one revises, such as a draft revised with a
    reviewer's answers; it closes as replaced once this one is staged."""


class FixPrPayload(_Payload):
    """Changes an agent made to a PR, pushed by the maintainer who approves it, with
    their own account: as a PR into the PR's branch, or straight onto that branch.
    A merge of the base branch into the PR (with an agent's resolution of its
    conflicts) is the same action with ``merge_ref`` set."""

    title: str
    body: str
    patch: str
    """``git diff --binary`` of the change, on the reviewed PR's head commit."""
    base_sha: str
    target: Literal["pr", "branch"] = "pr"
    """``pr``: a new branch and a PR into the PR's branch. ``branch``: one commit on
    the PR's branch itself, which the approver must be allowed to push to; the
    approver chooses."""
    merge_ref: str | None = None
    """When set, the commit merges this branch of the base repository, at
    ``merge_sha``, into the PR: a merge commit, with ``resolved`` for the files that
    conflict. ``patch`` then only shows the resolution."""
    merge_sha: str | None = None
    resolved: dict[str, str] = Field(default_factory=dict)
    """The resolved content of each file the merge conflicts in, by path."""
    files: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    """The review findings this addresses, by id."""
    runtime: str | None = None
    requested_by: str | None = None


class LabReproducePayload(_Payload):
    """A reproduction of a PR on a test machine, as an agent planned it. Once it is
    approved (or the repository's rule runs it), the actuator opens a session on the
    machine with a copy of the code before the PR (``base``) and at its head
    (``head``), and the agent works there one command at a time, each command
    checked against the machine's rules (design 5.10)."""

    lab: str
    summary: str
    """What the reproduction checks, in plain words."""
    steps: list[str] = Field(default_factory=list)
    """The plan, step by step in words, that the approver reads."""
    setup: str = ""
    """From the one-script reproductions before sessions; kept so they still read."""
    script: str = ""
    expect: str = ""
    """What result would confirm what the PR claims, on each side."""
    needs_gpu: bool = False
    wants_gpu: bool = False
    """Lease an idle GPU when there is one; without one the session opens with the
    GPUs hidden (``CUDA_VISIBLE_DEVICES`` empty), so it never takes a busy one."""
    timeout_seconds: int = 1800
    sides: list[Literal["base", "head"]] = Field(
        default_factory=lambda: ["base", "head"]
    )
    runtime: str | None = None
    requested_by: str | None = None


class CodePrPayload(_Payload):
    """A change to the service's own code: the patch goes on ``branch`` from ``base_sha``."""

    branch: str
    title: str
    body: str
    patch: str = ""
    """``git diff --binary`` of the change; empty when ``branch`` already exists."""
    base_sha: str = ""
    requested_by: str | None = None
    thread_id: str | None = None
    """The chat thread the request came from, told about the PR."""


class CodeMergePayload(_Payload):
    """Merge a PR on the service's own repository, approved by one of its maintainers."""

    number: int
    approved_by: str
    method: str = "squash"


class ServiceDeployPayload(_Payload):
    """Run the service at ``sha`` of its repository's default branch."""

    sha: str
    approved_by: str


class NotifyPayload(_Payload):
    event: str
    text: str
    channel: str | None = None


PAYLOADS: dict[ActionKind, type[BaseModel]] = {
    ActionKind.LABEL_APPLY: LabelPayload,
    ActionKind.LABEL_CI_GATE: LabelPayload,
    ActionKind.REVIEW_POST: ReviewPostPayload,
    ActionKind.REVIEW_UPDATE: ReviewUpdatePayload,
    ActionKind.THREAD_REPLY: ThreadReplyPayload,
    ActionKind.COMMENT_UPSERT: CommentUpsertPayload,
    ActionKind.CI_RERUN_FAILED: RerunPayload,
    ActionKind.PR_REQUEST_REVIEWERS: RequestReviewersPayload,
    ActionKind.PR_APPROVE: ApprovePrPayload,
    ActionKind.PR_UPDATE_BRANCH: UpdateBranchPayload,
    ActionKind.PR_MERGE: MergePayload,
    ActionKind.PR_NUDGE: NudgePayload,
    ActionKind.PR_CONVERT_TO_DRAFT: PrStatePayload,
    ActionKind.PR_CLOSE: PrStatePayload,
    ActionKind.ISSUE_LABEL: IssueLabelPayload,
    ActionKind.ISSUE_COMMENT: IssueCommentPayload,
    ActionKind.LAB_RUN: LabRunPayload,
    ActionKind.MEMORY_COMMIT: MemoryCommitPayload,
    ActionKind.MEMORY_CHANGE: MemoryChangePayload,
    ActionKind.CODE_PR: CodePrPayload,
    ActionKind.PR_FIX: FixPrPayload,
    ActionKind.LAB_REPRODUCE: LabReproducePayload,
    ActionKind.LAB_REVIEW: LabReproducePayload,
    ActionKind.CODE_MERGE: CodeMergePayload,
    ActionKind.SERVICE_DEPLOY: ServiceDeployPayload,
    ActionKind.NOTIFY: NotifyPayload,
}

PUBLIC_TEXT_FIELDS: dict[ActionKind, tuple[str, ...]] = {
    ActionKind.REVIEW_POST: ("body", "comments[].body"),
    ActionKind.REVIEW_UPDATE: ("body",),
    ActionKind.THREAD_REPLY: ("body",),
    ActionKind.COMMENT_UPSERT: ("body",),
    ActionKind.PR_MERGE: ("subject", "body"),
    ActionKind.PR_NUDGE: ("body",),
    ActionKind.PR_CONVERT_TO_DRAFT: ("body",),
    ActionKind.PR_CLOSE: ("body",),
    ActionKind.ISSUE_COMMENT: ("body",),
    ActionKind.PR_FIX: ("title", "body"),
    ActionKind.PR_APPROVE: ("body",),
}
"""Payload fields that become public text and must pass the leak guard."""


def make_action(
    kind: ActionKind, repo: str, payload: BaseModel, **kwargs: Any
) -> Action:
    """Build an action from a typed payload, validating it against its kind."""
    expected = PAYLOADS[kind]
    if not isinstance(payload, expected):
        raise TypeError(
            f"{kind} expects {expected.__name__}, got {type(payload).__name__}"
        )
    return Action(
        kind=kind, repo=repo, payload=payload.model_dump(mode="json"), **kwargs
    )
