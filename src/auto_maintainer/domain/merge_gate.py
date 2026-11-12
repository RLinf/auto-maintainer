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
) -> GateResult:
    """Decide whether ``pr`` can merge now, should update its branch, or must wait.

    ``reviewer_permissions`` maps each reviewer's login to their permission;
    ``blocking_checks`` maps each blocking hygiene check to whether it passed.
    """
    items: list[GateItem] = []
    ids = {i.lower() for i in service_identities}

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
    items.append(GateItem(name="not_draft", ok=not pr.is_draft))
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
    for name in sorted(required_names):
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
    for check_id, passed in sorted(blocking_checks.items()):
        items.append(GateItem(name=f"hygiene:{check_id}", ok=passed))

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


def squash_message(pr: PrSnapshot, profile: RepoProfile) -> tuple[str, str]:
    """The merge subject and body: commit subjects, then preserved trailers."""
    subject = profile.merge.message.subject.format(title=pr.title, number=pr.number)
    keep = {t.lower() for t in profile.merge.message.preserve_trailers}
    subjects = [f"* {c.subject}" for c in pr.commits if c.subject]
    trailers: list[str] = []
    for c in pr.commits:
        for key, value in c.trailers:
            line = f"{key}: {value}"
            if key.lower() in keep and line not in trailers:
                trailers.append(line)
    body = "\n".join(subjects)
    if trailers:
        body = f"{body}\n\n" + "\n".join(trailers) if body else "\n".join(trailers)
    return subject, body
