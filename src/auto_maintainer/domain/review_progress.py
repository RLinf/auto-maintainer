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

"""How a PR's review goes, for its owners (design 5.13): who reviewed it and how,
how much of each reviewer's feedback is addressed, and the timeline of reviews and
pushes.

Feedback is the review threads a reviewer opened on the PR's lines; a thread is
addressed once it is resolved, or once a comment check (``playbooks/comment_check``)
found the code now does what it asks (``fixed``, counted apart from resolved). A
thread whose lines changed since (outdated) but that nobody resolved or checked still
counts as open; the report counts those apart, as likely answered by a push. A review without threads (a
verdict with a message) has nothing to resolve: its reviewer's latest verdict, and
whether it was given at the current head, say where they stand. Threads the PR's
author opened are notes, not feedback, and count nowhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .ids import same_commit
from .records import PrAssignee
from .snapshot import PrSnapshot, ReviewEventState, ReviewThread

SAME_REVIEW_SECONDS = 120
"""A thread whose first comment is this close before a review belongs to it: GitHub
stamps a review's comments when they are written and the review when it is sent."""


CheckStatus = Literal["addressed", "partly", "open", "unclear"]


class CommentCheck(BaseModel):
    """What a comment check found of one review thread, at one head."""

    thread_id: str
    status: CheckStatus
    """``addressed``: the code now does what the comment asks, or its concern no
    longer applies; ``partly``; ``open``; ``unclear``: a question or discussion the
    code cannot settle."""
    summary: str = ""
    """What the comment asks, in one line."""
    reason: str = ""
    fixed_in: str | None = None
    head_sha: str
    checked_at: datetime


def checks_key(repo: str, number: int) -> str:
    """The store state key of a PR's comment checks, by thread id."""
    return f"comment_checks:{repo}#{number}"


def load_checks(data: Any) -> dict[str, CommentCheck]:
    """The checks in a stored state value; what does not parse is dropped."""
    out: dict[str, CommentCheck] = {}
    for key, value in (data or {}).items() if isinstance(data, dict) else []:
        try:
            out[key] = CommentCheck.model_validate(value)
        except ValueError:
            continue
    return out


class CommentItem(BaseModel):
    """One review thread a reviewer opened, as their progress row lists it."""

    thread_id: str
    path: str | None = None
    line: int | None = None
    url: str = ""
    excerpt: str = ""
    """The comment's first line, for when no check summarized it yet."""
    resolved: bool = False
    """Resolved on the forge."""
    outdated: bool = False
    check: CommentCheck | None = None

    @property
    def done(self) -> bool:
        return self.resolved or (
            self.check is not None and self.check.status == "addressed"
        )


class ReviewerProgress(BaseModel):
    """One reviewer of a PR and how far their feedback is addressed."""

    login: str
    verdict: str | None = None
    """Their latest review that is not a comment (``APPROVED``,
    ``CHANGES_REQUESTED``, ``DISMISSED``), else ``COMMENTED`` when they only
    commented; ``None`` before any review."""
    at_head: bool = False
    """Whether that verdict was given at the PR's current head."""
    reviews: int = 0
    threads: int = 0
    resolved: int = 0
    outdated: int = 0
    """Open threads whose lines changed since: likely answered, not yet resolved."""
    fixed: int = 0
    """Open threads a comment check found addressed in the code."""
    comments: list[CommentItem] = Field(default_factory=list)
    last_at: datetime | None = None
    assigned: bool = False
    """A reviewer of the PR on the dashboard."""
    requested: bool = False
    """GitHub asks them for a review now."""

    @property
    def addressed(self) -> float | None:
        return (self.resolved + self.fixed) / self.threads if self.threads else None


class ProgressEvent(BaseModel):
    """One step in a PR's review, oldest first in ``ReviewProgress.timeline``."""

    at: datetime | None = None
    kind: Literal["opened", "role", "review", "commits", "check"]
    login: str
    state: str | None = None
    """A review's verdict; a role event's role."""
    threads: int = 0
    """Threads a review opened."""
    resolved: int = 0
    """Of those, how many are resolved now."""
    commits: int = 0
    detail: str = ""


class ReviewProgress(BaseModel):
    """Where a PR's review stands, for the PR page and the PR list."""

    threads: int = 0
    resolved: int = 0
    outdated: int = 0
    fixed: int = 0
    approved_by: list[str] = Field(default_factory=list)
    """Reviewers whose latest verdict is an approval."""
    approved_at_head: list[str] = Field(default_factory=list)
    changes_requested_by: list[str] = Field(default_factory=list)
    reviewers: list[ReviewerProgress] = Field(default_factory=list)
    timeline: list[ProgressEvent] = Field(default_factory=list)

    @property
    def addressed(self) -> float | None:
        return (self.resolved + self.fixed) / self.threads if self.threads else None

    def summary(self) -> dict[str, object]:
        """The few numbers the PR list shows."""
        return {
            "threads": self.threads,
            "resolved": self.resolved,
            "fixed": self.fixed,
            "addressed": self.addressed,
            "approved": len(self.approved_by),
            "approved_at_head": len(self.approved_at_head),
            "changes_requested": len(self.changes_requested_by),
            "reviewed": sum(1 for r in self.reviewers if r.verdict),
            "reviewers": len(self.reviewers),
        }


def _opener(thread: ReviewThread) -> str | None:
    return thread.comments[0].author if thread.comments else None


def _opened_at(thread: ReviewThread) -> datetime | None:
    return thread.comments[0].created_at if thread.comments else None


def _comment_url(pr: PrSnapshot, comment_id: int) -> str:
    anchor = "note_" if "/-/merge_requests/" in pr.url else "discussion_r"
    return f"{pr.url}#{anchor}{comment_id}" if pr.url else ""


def review_progress(
    pr: PrSnapshot,
    assignees: list[PrAssignee] | None = None,
    checks: dict[str, CommentCheck] | None = None,
) -> ReviewProgress:
    """How ``pr``'s review goes: each reviewer, their verdict and how many of their
    threads are resolved, and a timeline of the PR's opening, the roles people took
    on the dashboard, each review with the threads it opened and how many are
    resolved now, and the pushes between reviews. Its reviewers are the ones assigned
    on the dashboard, first, then anyone else who reviewed it or opened a thread on
    it; someone GitHub only asks, who has not reviewed, is not one."""
    assignees = assignees or []
    checks = checks or {}
    author = pr.author.lower()
    people: dict[str, ReviewerProgress] = {}

    def person(login: str) -> ReviewerProgress:
        return people.setdefault(login.lower(), ReviewerProgress(login=login))

    reviews = sorted(
        (
            r
            for r in pr.reviews
            if r.state != ReviewEventState.PENDING and r.author.lower() != author
        ),
        key=lambda r: (r.submitted_at is None, r.submitted_at),
    )
    for review in reviews:
        p = person(review.author)
        p.reviews += 1
        if review.state != ReviewEventState.COMMENTED:
            p.verdict = review.state.value
            p.at_head = same_commit(review.commit_sha, pr.head_sha)
        elif p.verdict is None:
            p.verdict = ReviewEventState.COMMENTED.value
            p.at_head = same_commit(review.commit_sha, pr.head_sha)
        if review.submitted_at and (
            p.last_at is None or review.submitted_at > p.last_at
        ):
            p.last_at = review.submitted_at

    threads = [t for t in pr.threads if (_opener(t) or author).lower() != author]
    for thread in threads:
        p = person(_opener(thread) or "")
        p.threads += 1
        check = checks.get(thread.id)
        if thread.is_resolved:
            p.resolved += 1
        elif check is not None and check.status == "addressed":
            p.fixed += 1
        elif thread.is_outdated:
            p.outdated += 1
        first = thread.comments[0] if thread.comments else None
        p.comments.append(
            CommentItem(
                thread_id=thread.id,
                path=thread.path,
                line=thread.line,
                url=_comment_url(pr, first.id) if first else "",
                excerpt=(first.body.strip().splitlines() or [""])[0][:160]
                if first
                else "",
                resolved=thread.is_resolved,
                outdated=thread.is_outdated,
                check=check,
            )
        )
        for c in thread.comments:
            if (
                c.author.lower() == p.login.lower()
                and c.created_at
                and (p.last_at is None or c.created_at > p.last_at)
            ):
                p.last_at = c.created_at

    for a in assignees:
        if a.role == "reviewer" and a.login.lower() != author:
            person(a.login).assigned = True
    asked = {login.lower() for login in pr.requested_reviewers}
    for p in people.values():
        p.requested = p.login.lower() in asked

    out = ReviewProgress(
        threads=sum(p.threads for p in people.values()),
        resolved=sum(p.resolved for p in people.values()),
        outdated=sum(p.outdated for p in people.values()),
        fixed=sum(p.fixed for p in people.values()),
        approved_by=[
            p.login for p in people.values() if p.verdict == ReviewEventState.APPROVED
        ],
        approved_at_head=[
            p.login
            for p in people.values()
            if p.verdict == ReviewEventState.APPROVED and p.at_head
        ],
        changes_requested_by=[
            p.login
            for p in people.values()
            if p.verdict == ReviewEventState.CHANGES_REQUESTED
        ],
        reviewers=sorted(
            people.values(),
            key=lambda p: (not p.assigned, p.verdict is None, p.login.lower()),
        ),
    )
    out.timeline = _timeline(pr, assignees, reviews, threads, checks)
    return out


def _timeline(
    pr: PrSnapshot,
    assignees: list[PrAssignee],
    reviews: list,
    threads: list[ReviewThread],
    checks: dict[str, CommentCheck],
) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    runs: dict[datetime, list[CommentCheck]] = {}
    for check in checks.values():
        runs.setdefault(check.checked_at, []).append(check)
    for at, found in runs.items():
        events.append(
            ProgressEvent(
                at=at,
                kind="check",
                login="",
                threads=len(found),
                resolved=sum(1 for c in found if c.status == "addressed"),
                detail=found[0].head_sha[:7],
            )
        )
    if pr.created_at:
        events.append(ProgressEvent(at=pr.created_at, kind="opened", login=pr.author))
    for a in assignees:
        how = "claimed" if a.kind == "claimed" else f"assigned by {a.by}"
        events.append(
            ProgressEvent(at=a.at, kind="role", login=a.login, state=a.role, detail=how)
        )
    # Each review takes the threads its author opened since their previous review.
    unclaimed = list(threads)
    for review in reviews:
        mine = [
            t
            for t in unclaimed
            if (_opener(t) or "").lower() == review.author.lower()
            and (
                review.submitted_at is None
                or (opened := _opened_at(t)) is None
                or (opened - review.submitted_at).total_seconds() <= SAME_REVIEW_SECONDS
            )
        ]
        unclaimed = [t for t in unclaimed if t not in mine]
        events.append(
            ProgressEvent(
                at=review.submitted_at,
                kind="review",
                login=review.author,
                state=review.state.value,
                threads=len(mine),
                resolved=sum(1 for t in mine if t.is_resolved),
            )
        )
    for t in unclaimed:  # threads outside any review, such as single comments
        events.append(
            ProgressEvent(
                at=_opened_at(t),
                kind="review",
                login=_opener(t) or "",
                state=ReviewEventState.COMMENTED.value,
                threads=1,
                resolved=1 if t.is_resolved else 0,
            )
        )
    events.sort(key=lambda e: (e.at is None, e.at))
    # Pushes between steps, one event for each run of commits.
    commits = sorted(
        (c for c in pr.commits if c.committed_at),
        key=lambda c: c.committed_at,  # type: ignore[arg-type,return-value]
    )
    merged: list[ProgressEvent] = []
    i = 0
    for event in [*events, None]:
        run = []
        while i < len(commits) and (
            event is None or event.at is None or commits[i].committed_at <= event.at  # type: ignore[operator]
        ):
            run.append(commits[i])
            i += 1
        if run and event is not None and event.kind == "opened":
            event.commits = len(run)  # the commits it was opened with
        elif run:
            merged.append(
                ProgressEvent(
                    at=run[-1].committed_at,
                    kind="commits",
                    login=run[-1].author_login or pr.author,
                    commits=len(run),
                    detail=run[-1].subject[:120],
                )
            )
        if event is not None:
            merged.append(event)
    return merged
