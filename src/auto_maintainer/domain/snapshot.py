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

"""Read models of forge state: repositories, pull requests, issues, and checks.

A ``PrSnapshot`` is one consistent read of a pull request at one head SHA. Forge
adapters build snapshots; everything else only reads them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.+?)\s*$")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class RepoInfo(_Model):
    """Repository-level facts the service needs."""

    name: str
    default_branch: str = "main"
    default_branch_sha: str | None = None
    private: bool = False
    labels: list[str] = Field(default_factory=list)
    required_checks: list[str] | None = None
    """Check names that branch protection requires; ``None`` when unreadable."""
    requires_up_to_date: bool | None = None
    required_approvals: int | None = None


class PrFile(_Model):
    path: str
    additions: int = 0
    deletions: int = 0
    change_type: str = "MODIFIED"


class Commit(_Model):
    sha: str
    message: str = ""
    author_login: str | None = None
    author_email: str | None = None

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0] if self.message else ""

    @property
    def trailers(self) -> list[tuple[str, str]]:
        """``(key, value)`` pairs from the last paragraph of the message."""
        paragraphs = [p for p in self.message.strip().split("\n\n") if p.strip()]
        if len(paragraphs) < 2:
            return []
        found = []
        for line in paragraphs[-1].splitlines():
            m = _TRAILER_RE.match(line.strip())
            if m:
                found.append((m.group(1), m.group(2)))
        return found


class ReviewEventState(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"
    PENDING = "PENDING"


class Review(_Model):
    id: int
    author: str
    state: ReviewEventState
    body: str = ""
    submitted_at: datetime | None = None
    commit_sha: str | None = None
    author_permission: str | None = None
    """The reviewer's role on the repository, when the adapter knows it."""


class IssueComment(_Model):
    id: int
    author: str
    body: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ThreadComment(_Model):
    id: int
    author: str
    body: str = ""
    commit_sha: str | None = None
    created_at: datetime | None = None


class ReviewThread(_Model):
    id: str
    is_resolved: bool = False
    is_outdated: bool = False
    path: str | None = None
    line: int | None = None
    comments: list[ThreadComment] = Field(default_factory=list)


class CheckStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CheckConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    STALE = "stale"
    STARTUP_FAILURE = "startup_failure"


class CheckRun(_Model):
    """One check on the head commit, usually a CI job."""

    id: int
    name: str
    workflow: str | None = None
    status: CheckStatus = CheckStatus.QUEUED
    conclusion: CheckConclusion | None = None
    run_id: int | None = None
    run_attempt: int | None = None
    details_url: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    summary: str | None = None

    @property
    def awaiting_approval(self) -> bool:
        """A workflow GitHub holds until a maintainer approves running it; it has
        not run, so it neither passed nor failed."""
        return self.conclusion == CheckConclusion.ACTION_REQUIRED

    @property
    def failed(self) -> bool:
        return self.conclusion in {
            CheckConclusion.FAILURE,
            CheckConclusion.TIMED_OUT,
            CheckConclusion.STARTUP_FAILURE,
        }


class Mergeable(StrEnum):
    MERGEABLE = "MERGEABLE"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class PrState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class PrSnapshot(_Model):
    """A pull request at one head SHA."""

    repo: str
    number: int
    title: str
    body: str = ""
    author: str
    author_association: str = "NONE"
    is_draft: bool = False
    state: PrState = PrState.OPEN
    base_ref: str = "main"
    head_ref: str = ""
    head_sha: str
    head_repo: str | None = None
    url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    files: list[PrFile] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    comments: list[IssueComment] = Field(default_factory=list)
    threads: list[ReviewThread] = Field(default_factory=list)
    checks: list[CheckRun] = Field(default_factory=list)
    mergeable: Mergeable = Mergeable.UNKNOWN
    behind_base: bool | None = None
    additions: int = 0
    deletions: int = 0
    requested_reviewers: list[str] = Field(default_factory=list)
    """People asked to review it on the forge; teams are in ``requested_teams``."""
    requested_teams: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    """People the forge lists as the PR's assignees."""

    def fingerprint(self) -> str:
        """A hash of the fields whose change produces an event."""
        data = {
            "head": self.head_sha,
            "title": self.title,
            "body": self.body,
            "draft": self.is_draft,
            "state": self.state,
            "labels": sorted(self.labels),
            "reviews": [r.id for r in self.reviews],
            "comments": [
                (c.id, c.updated_at.isoformat() if c.updated_at else "")
                for c in self.comments
            ],
            "threads": [(t.id, t.is_resolved, len(t.comments)) for t in self.threads],
            "checks": [(c.id, c.status, c.conclusion) for c in self.checks],
            "mergeable": self.mergeable,
        }
        return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()[:20]

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


class IssueSnapshot(_Model):
    repo: str
    number: int
    title: str
    body: str = ""
    author: str
    labels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    url: str = ""
    comments: list[IssueComment] = Field(default_factory=list)


class MergedPrSummary(_Model):
    """A merged PR, for learning a repository's conventions during onboarding."""

    number: int
    title: str
    author: str
    labels: list[str] = Field(default_factory=list)
    merged_at: datetime | None = None
    commit_messages: list[str] = Field(default_factory=list)
    review_count: int = 0


class RunnerInfo(_Model):
    name: str
    status: str
    busy: bool = False
    labels: list[str] = Field(default_factory=list)


class CommentRef(_Model):
    id: int
    url: str = ""
