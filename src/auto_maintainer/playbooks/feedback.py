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

"""What reviewers turned down on a PR, for the agents that propose there next.

A rejected proposal and its reason go into the next prompts for the same PR
(``rejections_block``), and the labels reviewers turned down are not proposed for
it again (``declined_labels``): those in a rejected label proposal, and those a
reviewer removed from one before approving it (``Proposal.declined``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.actions import ActionKind, ProposalState
from ..domain.ports import Store
from .prompts import untrusted

LABEL_KINDS = frozenset({ActionKind.LABEL_APPLY})
EXCERPT_CHARS = 600
"""How much of a rejected text the next agent sees."""


@dataclass(frozen=True)
class Rejection:
    """One proposal a reviewer turned down on a PR."""

    kind: str
    labels: tuple[str, ...]
    """For a label proposal: the labels turned down."""
    excerpt: str
    """For a text proposal (a review, a comment): its beginning."""
    reason: str
    by: str
    at: datetime | None
    head_sha: str | None


async def rejections_on(store: Store, repo: str, number: int) -> list[Rejection]:
    """The proposals reviewers rejected on ``repo#number``, and the labels they
    removed from approved ones, oldest first."""
    out: list[Rejection] = []
    for state in (ProposalState.REJECTED, ProposalState.EXECUTED):
        for proposal in await store.list_proposals(state, repo):
            if state == ProposalState.EXECUTED and not proposal.declined:
                continue
            record = await store.get_action(proposal.action_id)
            if record is None or record.action.number != number:
                continue
            action = record.action
            payload = action.payload
            if state == ProposalState.EXECUTED:
                labels, reason = tuple(proposal.declined), "removed before approving"
            else:
                labels = tuple(payload.get("labels") or ())
                reason = proposal.reason or ""
            body = payload.get("body")
            out.append(
                Rejection(
                    kind=str(action.kind),
                    labels=labels if action.kind in LABEL_KINDS else (),
                    excerpt=body[:EXCERPT_CHARS] if isinstance(body, str) else "",
                    reason=reason,
                    by=proposal.decided_by or "",
                    at=proposal.decided_at,
                    head_sha=action.head_sha,
                )
            )
    return sorted(out, key=lambda r: r.at.timestamp() if r.at else 0.0)


def declined_labels(rejections: list[Rejection]) -> set[str]:
    """Labels reviewers turned down on the PR; none of them is proposed again."""
    return {label for r in rejections for label in r.labels}


def rejections_block(rejections: list[Rejection]) -> str:
    """The prompt part that tells the agent what reviewers turned down on this PR
    and why, or nothing when they turned nothing down."""
    if not rejections:
        return ""
    lines = []
    for r in rejections:
        what = (
            f"labels {', '.join(r.labels)}"
            if r.labels
            else f"{r.kind}" + (f" beginning: {r.excerpt}" if r.excerpt else "")
        )
        at = f" at {r.head_sha[:7]}" if r.head_sha else ""
        lines.append(
            f"- {r.by or 'a reviewer'} turned down {what}{at}. Why: {r.reason}"
        )
    return (
        "Reviewers of this repository turned these proposals down on this PR. Do not "
        "propose them again, and do not repeat what their reasons object to, unless "
        "the PR changed in a way that answers the reason:\n"
        + untrusted("reviewers.rejections", "\n".join(lines), 6000)
    )
