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

"""The merge gate (design 5.5), as a pure function of a PR and its context."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .clock import as_utc
from .ids import same_commit
from .permissions import Permission
from .profile import RepoProfile
from .run_ci import latest_checks
from .snapshot import (
    CheckConclusion,
    CheckStatus,
    Commit,
    Mergeable,
    PrSnapshot,
    RepoInfo,
    ReviewEventState,
)

_PASSING = {CheckConclusion.SUCCESS, CheckConclusion.NEUTRAL, CheckConclusion.SKIPPED}
AWAITING_APPROVAL = "waiting for a maintainer to approve running it on GitHub"


class MergeArm(BaseModel):
    repo: str
    number: int
    sha: str
    armed_by: str
    armed_at: datetime
    armer_permission: Permission = Permission.NONE
    armer_is_codeowner: bool = False
    position: float | None = None
    """Where it stands in the repository's merge schedule, lowest first; ``None``
    keeps its arming time (``merge_schedule.order``)."""
    auto: bool = False
    """Armed by automatic scheduling (``merge.schedule.auto``) as ``armed_by``,
    who never saw this PR: a step that runs outside code with the repository's
    secrets (approving a fork's held workflows) waits for a person."""
    unseen: bool = False
    """Its head has commits pushed after it was scheduled, which ``armed_by`` has not
    seen (the arm follows them, ``playbooks/merge.py:_new_head``): as for an
    automatic arm, a step that runs outside code with the repository's secrets, or
    pushes an agent's change as ``armed_by``, waits for them. Cleared on a hand-made
    arm once ``armed_by`` approves held CI at the head or pushes a ``pr.fix`` onto
    the branch as themselves."""
    forced: bool = False
    """Scheduled by hand before it met the repository's target: its CI runs on its
    turn whatever the target says, and it merges once the merge rules hold."""


class GateItem(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    pending: bool = False
    """Not satisfied yet, but may become so without anyone acting (CI running)."""


class GateResult(BaseModel):
    outcome: Literal["merge", "update_branch", "wait", "blocked"]
    items: list[GateItem] = Field(default_factory=list)
    needs_ci_gate: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome == "merge"

    def failing(self) -> list[GateItem]:
        return [i for i in self.items if not i.ok]


def is_bot(login: str) -> bool:
    """Whether ``login`` is a bot's account (a GitHub App), never a person's."""
    return login.lower().endswith("[bot]")


def _latest_review_by_author(pr: PrSnapshot) -> dict[str, ReviewEventState]:
    latest: dict[str, tuple[datetime | None, ReviewEventState]] = {}
    for r in pr.reviews:
        if r.state in (ReviewEventState.PENDING, ReviewEventState.COMMENTED):
            continue
        prev = latest.get(r.author)
        if prev is None or (
            r.submitted_at and (prev[0] is None or r.submitted_at >= prev[0])
        ):
            latest[r.author] = (r.submitted_at, r.state)
    return {a: s for a, (_, s) in latest.items()}


def evaluate_merge_gate(
    pr: PrSnapshot,
    profile: RepoProfile,
    repo_info: RepoInfo,
    arm: MergeArm | None,
    reviewer_permissions: dict[str, Permission],
    service_identities: set[str],
    blocking_checks: dict[str, bool],
    paused: bool,
    now: datetime,
    *,
    blocking_details: dict[str, str] | None = None,
) -> GateResult:
    """Decide whether ``pr`` can merge now, should update its branch, or must wait.

    ``reviewer_permissions`` maps each reviewer's login to their permission;
    ``blocking_checks`` maps each blocking hygiene check to whether it passed.
    Approvals count from people with write access. One from a bot account never
    counts, the service's own among them; the service's login counts when it is a
    person's account (a deployment acting as its owner), since the service itself
    never approves: its reviews only comment or request changes, and an approval
    runs only as the person who pressed it.
    """
    items: list[GateItem] = []
    ids = {i.lower() for i in service_identities if is_bot(i)}

    # 1. Armed by someone allowed, at the current head.
    who = profile.merge.arming.who
    if arm is None:
        items.append(
            GateItem(name="armed", ok=False, detail="no maintainer armed this merge")
        )
    else:
        allowed = (
            arm.armer_is_codeowner
            if who == "codeowners"
            else arm.armer_permission.at_least(Permission(who))
        )
        current = same_commit(arm.sha, pr.head_sha)
        detail = f"armed by {arm.armed_by} at {arm.sha[:7]}"
        if not allowed:
            detail = f"{arm.armed_by} may not arm merges (needs {who})"
        elif not current:
            detail = f"armed at {arm.sha[:7]}, but the head is now {pr.head_sha[:7]}"
        items.append(GateItem(name="armed", ok=allowed and current, detail=detail))

    # 2. Human approvals, and no outstanding request for changes.
    latest = _latest_review_by_author(pr)
    approvers = [
        a
        for a, s in latest.items()
        if s == ReviewEventState.APPROVED
        and a.lower() not in ids
        and not is_bot(a)
        and reviewer_permissions.get(a, Permission.NONE).at_least(Permission.WRITE)
    ]
    needed = max(profile.merge.approvals, repo_info.required_approvals or 0)
    # GitHub's own verdict on the branch's review rules covers what the count does
    # not: a code owner's review, an approval of the last push.
    forge_waits = pr.review_decision == "REVIEW_REQUIRED"
    items.append(
        GateItem(
            name="approvals",
            ok=len(approvers) >= needed and not forge_waits,
            detail=f"{len(approvers)} of {needed} approvals"
            + (f" ({', '.join(sorted(approvers))})" if approvers else "")
            + (
                "; GitHub still waits for a review its rules require"
                + (" (a code owner's)" if repo_info.requires_code_owner_reviews else "")
                if forge_waits and len(approvers) >= needed
                else ""
            ),
        )
    )
    blockers = [a for a, s in latest.items() if s == ReviewEventState.CHANGES_REQUESTED]
    items.append(
        GateItem(
            name="no_changes_requested",
            ok=not blockers,
            detail=f"changes requested by {', '.join(sorted(blockers))}"
            if blockers
            else "",
        )
    )

    # 3. Not a draft, no blocking labels, no unresolved threads.
    items.append(
        GateItem(
            name="not_draft",
            ok=not pr.is_draft,
            detail="draft PR" if pr.is_draft else "",
        )
    )
    blocking_labels = {profile.label_for(r) for r in profile.merge.blocking_roles} - {
        None
    }
    present = sorted(blocking_labels & set(pr.labels))
    items.append(
        GateItem(name="no_blocking_labels", ok=not present, detail=", ".join(present))
    )
    unresolved = [t for t in pr.threads if not t.is_resolved]
    items.append(
        GateItem(
            name="threads_resolved",
            ok=not unresolved,
            detail=f"{len(unresolved)} unresolved" if unresolved else "",
        )
    )

    # 4. Required checks, from the newest run of each workflow.
    checks = latest_checks(pr.checks)
    needs_gate = False
    required_names = set(repo_info.required_checks or [])
    optional = {c.lower() for c in profile.ci.start.optional_checks}
    for name in sorted(required_names):
        if name.lower() in optional:
            # The repository merges without it (a sign-off check that only warns).
            continue
        runs = [c for c in checks if c.name == name]
        if not runs:
            items.append(
                GateItem(
                    name=f"check:{name}",
                    ok=False,
                    pending=True,
                    detail="not reported yet",
                )
            )
        elif any(c.awaiting_approval for c in runs):
            items.append(
                GateItem(
                    name=f"check:{name}",
                    ok=False,
                    pending=True,
                    detail=AWAITING_APPROVAL,
                )
            )
        elif any(c.status != CheckStatus.COMPLETED for c in runs):
            items.append(
                GateItem(name=f"check:{name}", ok=False, pending=True, detail="running")
            )
        else:
            ok = all(c.conclusion in _PASSING for c in runs)
            items.append(
                GateItem(
                    name=f"check:{name}",
                    ok=ok,
                    pending=not ok,
                    detail="" if ok else "failed",
                )
            )
    for wf in profile.ci.required_workflows:
        runs = [c for c in checks if c.workflow == wf]
        if not runs:
            gate_label = profile.label_for("ci_gate")
            needs_gate = bool(gate_label and gate_label not in pr.labels)
            detail = (
                f"waiting for the {gate_label!r} label" if needs_gate else "has not run"
            )
            items.append(
                GateItem(name=f"workflow:{wf}", ok=False, pending=True, detail=detail)
            )
            continue
        if any(c.awaiting_approval for c in runs):
            items.append(
                GateItem(
                    name=f"workflow:{wf}",
                    ok=False,
                    pending=True,
                    detail=AWAITING_APPROVAL,
                )
            )
            continue
        running = [c for c in runs if c.status != CheckStatus.COMPLETED]
        bad = [
            c
            for c in runs
            if c.status == CheckStatus.COMPLETED and c.conclusion not in _PASSING
        ]
        succeeded = [c for c in runs if c.conclusion == CheckConclusion.SUCCESS]
        if running:
            items.append(
                GateItem(
                    name=f"workflow:{wf}",
                    ok=False,
                    pending=True,
                    detail=f"{len(running)} jobs running",
                )
            )
        elif bad or not succeeded:
            names = ", ".join(c.name for c in bad) or "every job was skipped"
            items.append(
                GateItem(
                    name=f"workflow:{wf}",
                    ok=False,
                    pending=True,
                    detail=f"failed: {names}",
                )
            )
        else:
            items.append(GateItem(name=f"workflow:{wf}", ok=True))

    # 5. Blocking hygiene checks.
    details = blocking_details or {}
    for check_id, passed in sorted(blocking_checks.items()):
        items.append(
            GateItem(
                name=f"hygiene:{check_id}",
                ok=passed,
                detail="" if passed else details.get(check_id, ""),
            )
        )

    # 6. Conflicts and branch currency.
    conflict = pr.mergeable == Mergeable.CONFLICTING
    items.append(
        GateItem(
            name="no_conflict",
            ok=not conflict,
            detail="conflicts with the base branch" if conflict else "",
        )
    )
    behind = bool(pr.behind_base) and (
        bool(repo_info.requires_up_to_date) or profile.merge.update_branch
    )

    # 7. Pause and freezes.
    frozen = [
        f for f in profile.merge.freezes if as_utc(f.start) <= now < as_utc(f.end)
    ]
    items.append(GateItem(name="not_paused", ok=not paused))
    items.append(
        GateItem(
            name="no_freeze", ok=not frozen, detail=frozen[0].reason if frozen else ""
        )
    )

    hard = [i for i in items if not i.ok and not i.pending]
    if hard:
        return GateResult(outcome="blocked", items=items, needs_ci_gate=needs_gate)
    if any(not i.ok for i in items):
        return GateResult(outcome="wait", items=items, needs_ci_gate=needs_gate)
    if behind:
        items.append(
            GateItem(
                name="up_to_date",
                ok=False,
                pending=True,
                detail="behind the base branch",
            )
        )
        return GateResult(outcome="update_branch", items=items)
    return GateResult(outcome="merge", items=items)


_EMAIL_RE = re.compile(r"<([^<>\s]+@[^<>\s]+)>")


def _trailer_email(value: str) -> str:
    """Lowercased email inside ``Name <email>``, or ``""`` when absent."""
    m = _EMAIL_RE.search(value)
    return m.group(1).strip().lower() if m else ""


def _commit_identity(c: Commit) -> tuple[str, str, str] | None:
    """``(login, name, email)`` of a human non-merge commit author, or ``None``."""
    login = (c.author_login or "").strip()
    if is_bot(login) or c.subject.startswith(("Merge branch ", "Merge remote-")):
        return None
    name = (c.author_name or "").strip() or login
    email = (c.author_email or "").strip() or (
        f"{login}@users.noreply.github.com" if login else ""
    )
    if (
        not name
        or not email
        or email.lower().endswith("[bot]@users.noreply.github.com")
    ):
        return None
    return login, name, email


def author_signoffs(pr: PrSnapshot) -> list[str]:
    """``Signed-off-by`` lines in the name of the PR's commit authors, as their
    commits name them, each once, in the order they first committed: never for a
    bot, nor for a merge of the base branch (the base's authors signed off their own
    commits there)."""
    lines: list[str] = []
    for c in pr.commits:
        ident = _commit_identity(c)
        if ident is None:
            continue
        _, name, email = ident
        line = f"Signed-off-by: {name} <{email}>"
        if line not in lines:
            lines.append(line)
    if not lines and pr.author and not is_bot(pr.author):
        lines.append(
            f"Signed-off-by: {pr.author} <{pr.author}@users.noreply.github.com>"
        )
    return lines


def author_coauthors(pr: PrSnapshot) -> list[str]:
    """``Co-authored-by`` lines for every human commit author on the PR other than
    the PR's author, each once in the order they first committed, matching GitHub's
    default squash attribution: never for a bot, nor for a merge of the base branch."""
    pr_author = (pr.author or "").strip().lower()
    pr_author_emails = {
        c.author_email.strip().lower()
        for c in pr.commits
        if pr_author
        and c.author_login
        and c.author_login.strip().lower() == pr_author
        and c.author_email
    }
    lines: list[str] = []
    seen_logins: set[str] = set()
    seen_emails: set[str] = set()
    for c in pr.commits:
        ident = _commit_identity(c)
        if ident is None:
            continue
        login, name, email = ident
        login_low = login.lower()
        email_low = email.lower()
        if pr_author and (
            (login_low and login_low == pr_author)
            or (not login_low and email_low in pr_author_emails)
        ):
            continue
        if email_low in seen_emails or (login_low and login_low in seen_logins):
            continue
        seen_emails.add(email_low)
        if login_low:
            seen_logins.add(login_low)
        lines.append(f"Co-authored-by: {name} <{email}>")
    return lines


def squash_message(pr: PrSnapshot, profile: RepoProfile) -> tuple[str, str]:
    """The merge subject and body: only preserved trailers (such as ``Signed-off-by``
    and ``Co-authored-by``, plus ``Co-authored-by`` for the PR's other human commit
    authors when kept, and with ``signoff_from_authors``, the authors' sign-offs
    when no commit has one). Individual commit subjects are omitted."""
    message = profile.merge.message
    subject = message.subject.format(title=pr.title, number=pr.number)
    keep = {t.lower() for t in message.preserve_trailers}
    other_trailers: list[str] = []
    coauthor_trailers: list[str] = []
    seen_trailers: set[tuple[str, str]] = set()
    seen_coauthor_emails: set[str] = set()
    for c in pr.commits:
        for key, value in c.trailers:
            key_low = key.lower()
            if key_low not in keep:
                continue
            dedup_key = (key_low, value.lower())
            if dedup_key in seen_trailers:
                continue
            line = f"{key}: {value}"
            if key_low == "co-authored-by":
                email = _trailer_email(value)
                if email and email in seen_coauthor_emails:
                    continue
                if email:
                    seen_coauthor_emails.add(email)
                seen_trailers.add(dedup_key)
                coauthor_trailers.append(line)
            else:
                seen_trailers.add(dedup_key)
                other_trailers.append(line)
    if (
        message.signoff_from_authors
        and "signed-off-by" in keep
        and not any(t.lower().startswith("signed-off-by:") for t in other_trailers)
    ):
        other_trailers = [*author_signoffs(pr), *other_trailers]
    if "co-authored-by" in keep:
        for line in author_coauthors(pr):
            email = _trailer_email(line)
            if email and email in seen_coauthor_emails:
                continue
            if email:
                seen_coauthor_emails.add(email)
            coauthor_trailers.append(line)
    trailers = [*other_trailers, *coauthor_trailers]
    return subject, "\n".join(trailers)
