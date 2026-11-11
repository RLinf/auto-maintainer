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

"""Whether a PR's reviews meet its base branch's rules, in people, as GitHub decides.

GitHub keeps two review gates on a protected branch, apart from each other: a number
of approving reviews from people with write access, and, when the branch asks for
it, an approval from a code owner of each changed file (any one of the owners on its
CODEOWNERS line; ``codeowners.code_owner_report``). A reviewer's latest review is
the one that counts, and the author's own never does. A request for changes holds
the merge until that reviewer approves or the review is dismissed. When the branch
requires approval of the most recent push, an approval of an earlier commit does not
count. GitHub's own verdict (``PrSnapshot.review_decision``) is the word on whether
the PR passes; this says who approved and who is still missing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection

from pydantic import BaseModel, Field

from .codeowners import CodeOwnerReport
from .ids import same_commit
from .review_progress import ReviewProgress
from .snapshot import PrSnapshot, RepoInfo, ReviewEventState


class NotCounted(BaseModel):
    login: str
    reason: str
    """``no write access`` or ``approved an earlier commit``."""


class ApprovalStatus(BaseModel):
    decision: str | None = None
    """GitHub's verdict: ``APPROVED``, ``CHANGES_REQUESTED``, ``REVIEW_REQUIRED``."""
    required: int | None = None
    """Approving reviews the branch requires; ``None`` when it could not be read
    (a base branch other than the default)."""
    approved_by: list[str] = Field(default_factory=list)
    """Approvals that count toward ``required``."""
    not_counted: list[NotCounted] = Field(default_factory=list)
    missing: int | None = None
    """Approvals still needed to reach ``required``, from anyone with write access."""
    changes_requested_by: list[str] = Field(default_factory=list)
    code_owners_required: bool | None = None
    code_owners_waiting: list[list[str]] = Field(default_factory=list)
    """``CodeOwnerReport.waiting``: for each missing code-owner approval, the owners
    any one of whom may give it."""
    last_push_approval: bool | None = None


def approval_status(
    pr: PrSnapshot,
    info: RepoInfo | None,
    owners: CodeOwnerReport,
    writers: Collection[str] | None = None,
) -> ApprovalStatus:
    """Where ``pr``'s reviews stand against ``info``'s rules for its base branch
    (``None`` when they are not known). ``writers`` are the reviewers known to have
    write access, lowercased; ``None`` counts every approval."""
    latest: dict[str, tuple[str, str, bool]] = {}
    for review in pr.reviews:
        if review.state in (ReviewEventState.COMMENTED, ReviewEventState.PENDING):
            continue
        latest[review.author.lower()] = (
            review.author,
            review.state.value,
            same_commit(review.commit_sha, pr.head_sha),
        )
    author = pr.author.lower()
    last_push = bool(info and info.requires_last_push_approval)
    approved: list[str] = []
    not_counted: list[NotCounted] = []
    blockers: list[str] = []
    for key, (login, state, at_head) in latest.items():
        if state == ReviewEventState.CHANGES_REQUESTED.value:
            blockers.append(login)
        if state != ReviewEventState.APPROVED.value or key == author:
            continue
        if writers is not None and key not in writers:
            not_counted.append(NotCounted(login=login, reason="no write access"))
        elif last_push and not at_head:
            not_counted.append(
                NotCounted(login=login, reason="approved an earlier commit")
            )
        else:
            approved.append(login)
    required = info.required_approvals if info is not None else None
    return ApprovalStatus(
        decision=pr.review_decision,
        required=required,
        approved_by=approved,
        not_counted=not_counted,
        missing=max(0, required - len(approved)) if required is not None else None,
        changes_requested_by=blockers,
        code_owners_required=info.requires_code_owner_reviews if info else None,
        code_owners_waiting=owners.waiting,
        last_push_approval=info.requires_last_push_approval if info else None,
    )


class ApprovalSummary(BaseModel):
    """Where a PR's approvals stand, as its row and its page show them: one
    computation for both (``approval_summary``)."""

    approved: int
    """Approvals that count under the base branch's rules."""
    required: int | None
    """Approvals the branch requires; ``None`` when its rules are not known."""
    needed: int | None
    """Approvals still needed: the count the branch asks for, or the owners whose
    approvals would cover every code-owner part still waiting (``fewest_owners``),
    whichever is more; ``None`` when the rules are not known."""
    owners_waiting: int
    """Code-owner parts still waiting for an owner's approval, when the branch asks
    for them."""
    assigned: int
    """Reviewers assigned on the dashboard."""
    assigned_approved: int
    """Of them, those whose latest verdict is an approval."""
    assigned_waiting: list[str] = Field(default_factory=list)
    """Assigned reviewers who have not approved."""
    changes_requested: int = 0


def fewest_owners(waiting: list[list[str]]) -> int:
    """How many approvals cover every waiting code-owner part, each part needing one
    of its owners: one owner often owns several parts. Picks the owner of the most
    parts left until none is left, which gives approvals that do cover them all."""
    left = [{o.lower() for o in part} for part in waiting]
    count = sum(1 for part in left if not part)
    left = [part for part in left if part]
    while left:
        best = Counter(o for part in left for o in part).most_common(1)[0][0]
        left = [part for part in left if best not in part]
        count += 1
    return count


def approval_summary(
    status: ApprovalStatus, progress: ReviewProgress
) -> ApprovalSummary:
    """``status`` (``approval_status``) and the assigned reviewers of ``progress``
    as the few numbers a PR's row and page show."""
    waiting = status.code_owners_waiting if status.code_owners_required else []
    owners = len(waiting)
    known = status.required is not None or bool(status.code_owners_required)
    assigned = [r for r in progress.reviewers if r.assigned]
    approved = [r for r in assigned if r.verdict == ReviewEventState.APPROVED.value]
    return ApprovalSummary(
        approved=len(status.approved_by),
        required=status.required,
        needed=max(status.missing or 0, fewest_owners(waiting)) if known else None,
        owners_waiting=owners,
        assigned=len(assigned),
        assigned_approved=len(approved),
        assigned_waiting=[r.login for r in assigned if r not in approved],
        changes_requested=len(status.changes_requested_by),
    )
