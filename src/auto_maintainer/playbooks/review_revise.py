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

"""Revising a review or follow-up draft that waits for a decision (design 5.13, 5.10).

Two callers revise a draft: a reviewer's turn in the conversation about a review
(``web/review_talk.py``) and a reproduction that finished after its review was written
(``playbooks/reproduce.py``). Both give the agent the same picture of the review (the
PR and its diff, the repository's review skill and format, the reproduction on record,
the lessons, the review as structured data, and the draft as it would be posted, with
reviewers' edits), and both render what it returns as the review and follow-up
playbooks render theirs, so the findings a fix or a follow-up reads change with the
text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..domain.actions import ReviewPostPayload
from ..domain.agents import TaskKind
from ..domain.profile import RepoProfile
from ..domain.review import (
    FollowupResult,
    ReviewResult,
    render_followup,
    render_review,
)
from ..domain.snapshot import PrSnapshot
from .prompts import (
    pr_block,
    quality_block,
    review_skill_block,
    skill_format_block,
    skill_owns_format,
    template_block,
    untrusted,
    writing_block,
)


class ReviewRevision(BaseModel):
    reply: str = Field(
        description="Your answer to the reviewer, in the language they write in."
    )
    review: ReviewResult | None = Field(
        default=None,
        description="The whole review, revised, when this turn changes it; null when it stays as it is.",
    )
    lessons: list[str] = Field(
        default_factory=list,
        description="Rules for future reviews of this repository that the reviewer stated or agreed to.",
    )


class FollowupRevision(BaseModel):
    reply: str = Field(
        description="Your answer to the reviewer, in the language they write in."
    )
    followup: FollowupResult | None = Field(
        default=None,
        description="The whole follow-up, revised, when this turn changes it; null when it stays as it is.",
    )
    lessons: list[str] = Field(
        default_factory=list,
        description="Rules for future reviews of this repository that the reviewer stated or agreed to.",
    )


def task_kind_of(payload: ReviewPostPayload) -> TaskKind:
    """The task kind (and so the route) that writes and revises this draft."""
    return TaskKind.REVIEW if payload.kind == "review" else TaskKind.FOLLOWUP


def answer_model(kind: TaskKind) -> type[ReviewRevision] | type[FollowupRevision]:
    return ReviewRevision if kind == TaskKind.REVIEW else FollowupRevision


def names(kind: TaskKind) -> tuple[str, str]:
    """What the draft is called in a prompt, and the answer field that revises it."""
    return (
        ("review", "review")
        if kind == TaskKind.REVIEW
        else ("follow-up review", "followup")
    )


def draft_text(payload: ReviewPostPayload) -> str:
    """The draft as it would be posted now: the main comment, then each inline
    comment under its place in the diff."""
    parts = [payload.body]
    for c in payload.comments:
        lines = f"{c.start_line}-{c.line}" if c.start_line else str(c.line)
        parts.append(f"--- inline comment on {c.path}:{lines}\n{c.body}")
    return "\n\n".join(parts)


def owns_format(kind: TaskKind, profile: RepoProfile, workdir: Path) -> bool:
    return kind == TaskKind.REVIEW and skill_owns_format(profile, workdir)


def revision_blocks(
    *,
    kind: TaskKind,
    profile: RepoProfile,
    skills_dir: Path,
    workdir: Path,
    reproduction: str,
    lessons: str,
    pr: PrSnapshot,
    head: str,
    default_branch: str,
    diff: str,
    payload: ReviewPostPayload,
) -> list[str]:
    """Everything a revising agent reads after its instructions: the skill and the
    format, ``reproduction`` (a ``reproduction_block``), the lessons, the checkout, the
    PR, and the review as data and as text."""
    _, field = names(kind)
    if kind == TaskKind.REVIEW:
        format_block = (
            skill_format_block(profile)
            if owns_format(kind, profile, workdir)
            else template_block(profile)
        )
    else:
        format_block = "`recommendation` must be one of: " + ", ".join(
            repr(v) for v in profile.review.recommendations.values()
        )
    return [
        review_skill_block(profile, skills_dir, workdir),
        format_block,
        writing_block(profile),
        quality_block(profile, followup=kind != TaskKind.REVIEW),
        reproduction,
        f"Review lessons from this repository's maintainers:\n{lessons}"
        if lessons
        else "",
        f"The working directory is a checkout of the reviewed commit {head}. "
        f"Compare against origin/{default_branch}."
        + (
            f" The PR's head is now {pr.head_sha}: say so when it matters."
            if pr.head_sha != head
            else ""
        ),
        pr_block(pr, diff),
        untrusted(
            f"{field}.structured",
            json.dumps(payload.result or {}, ensure_ascii=False, indent=1),
            60_000,
        ),
        untrusted(f"{field}.draft", draft_text(payload), 60_000),
    ]


def apply_revision(
    answer: ReviewRevision | FollowupRevision,
    payload: ReviewPostPayload,
    profile: RepoProfile,
    pr: PrSnapshot,
    head_sha: str,
    own_format: bool,
) -> dict[str, Any] | None:
    """The draft's new payload when the answer revises the review, rendered as the
    playbook renders it; ``None`` when it does not."""
    if isinstance(answer, ReviewRevision):
        review = answer.review
        if review is None:
            return None
        meaning = profile.review.meaning_of(review.recommendation)
        if meaning is None:
            meaning = (
                "changes"
                if any(f.severity in ("critical", "major") for f in review.findings)
                else "ready"
            )
        review.recommendation = profile.review.recommendations[meaning]
        review.labels = []
        rendered = render_review(review, profile, set(pr.paths), own_format=own_format)
        result: dict[str, Any] = review.model_dump(mode="json")
        recommendation = review.recommendation
    else:
        followup = answer.followup
        if followup is None:
            return None
        if profile.review.meaning_of(followup.recommendation) is None:
            followup.recommendation = payload.recommendation or followup.recommendation
        followup.labels = []
        previous = (payload.result or {}).get("previous_sha")
        rendered = render_followup(followup, head_sha, previous, profile)
        result = {**followup.model_dump(mode="json"), "previous_sha": previous}
        recommendation = followup.recommendation
    if result.get("quality") is None and (payload.result or {}).get("quality"):
        # A revision that does not judge the quality again keeps the draft's.
        result["quality"] = (payload.result or {})["quality"]
    return payload.model_copy(
        update={
            "body": rendered.body,
            "comments": rendered.comments,
            "recommendation": recommendation,
            "result": result,
        }
    ).model_dump(mode="json")
