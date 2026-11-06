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

"""Review states, review results, and how they render (design 5.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .actions import InlineComment
from .ids import same_commit, short_sha
from .markers import REVIEW_KINDS, parse_markers
from .profile import RepoProfile
from .snapshot import PrSnapshot, PrState, ReviewEventState


class ReviewState(StrEnum):
    NEW_REVIEW = "NEW_REVIEW"
    FOLLOWUP_REVIEW = "FOLLOWUP_REVIEW"
    APPROVED_NEW_COMMITS = "APPROVED_NEW_COMMITS"
    UP_TO_DATE = "UP_TO_DATE"
    SELF_PR = "SELF_PR"
    DRAFT = "DRAFT"
    CLOSED = "CLOSED"


class ReviewClassification(BaseModel):
    state: ReviewState
    reviewed_sha: str | None = None
    latest_review_state: str | None = None
    latest_review_id: int | None = None
    latest_comment_id: int | None = None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def classify_review_state(
    pr: PrSnapshot,
    identities: set[str],
    legacy_markers: list[str] | None = None,
    include_drafts: bool = False,
    own_actions: frozenset[str] = frozenset(),
) -> ReviewClassification:
    """Classify where ``pr`` stands relative to the service's own reviews.

    ``identities`` are the logins whose reviews count as the service's (lowercase
    or not). Only reviews and comments carrying a review marker count; a dismissed
    review with no body does not. A review by anyone else counts too when its marker
    names one of ``own_actions``: a draft of this service that a maintainer posted
    as themselves. A marker alone never does, so nobody can fake a review.
    """
    if pr.state != PrState.OPEN:
        return ReviewClassification(state=ReviewState.CLOSED)
    if pr.is_draft and not include_drafts:
        return ReviewClassification(state=ReviewState.DRAFT)
    ids = {i.lower() for i in identities}
    if pr.author.lower() in ids:
        return ReviewClassification(state=ReviewState.SELF_PR)

    events: list[tuple[datetime, str, str | None, int | None, int | None]] = []

    def ours(author: str, body: str) -> bool:
        if author.lower() in ids:
            return True
        return bool(own_actions) and any(
            m.action in own_actions for m in parse_markers(body)
        )

    for r in pr.reviews:
        if not ours(r.author, r.body) or r.state == ReviewEventState.PENDING:
            continue
        if r.state == ReviewEventState.DISMISSED and not r.body.strip():
            continue
        sha = r.commit_sha
        for marker in parse_markers(r.body, legacy_markers):
            if marker.kind in REVIEW_KINDS and marker.sha:
                sha = marker.sha
        events.append((r.submitted_at or _EPOCH, r.state.value, sha, r.id, None))
    for c in pr.comments:
        if not ours(c.author, c.body):
            continue
        markers = [
            m for m in parse_markers(c.body, legacy_markers) if m.kind in REVIEW_KINDS
        ]
        if not markers:
            continue
        sha = next((m.sha for m in reversed(markers) if m.sha), None)
        events.append(
            (c.updated_at or c.created_at or _EPOCH, "COMMENTED", sha, None, c.id)
        )

    if not events:
        return ReviewClassification(state=ReviewState.NEW_REVIEW)
    events.sort(key=lambda e: e[0])
    _, latest_state, latest_sha, review_id, comment_id = events[-1]
    reviewed_sha = latest_sha or next((e[2] for e in reversed(events) if e[2]), None)
    current = same_commit(latest_sha, pr.head_sha)
    if latest_state == ReviewEventState.APPROVED:
        state = ReviewState.UP_TO_DATE if current else ReviewState.APPROVED_NEW_COMMITS
    else:
        state = ReviewState.UP_TO_DATE if current else ReviewState.FOLLOWUP_REVIEW
    return ReviewClassification(
        state=state,
        reviewed_sha=reviewed_sha,
        latest_review_state=latest_state,
        latest_review_id=review_id,
        latest_comment_id=comment_id,
    )


# ---------------------------------------------------------------------------
# Review results returned by agents.
# ---------------------------------------------------------------------------

Severity = Literal["critical", "major", "minor", "nit"]


class Finding(BaseModel):
    id: str = Field(description="Stable ID such as F1.")
    severity: Severity
    category: str = Field(
        description="correctness, design, reuse, tests, docs, metadata, or a profile category."
    )
    title: str
    body: str = Field(description="What is wrong, the trigger, and the evidence.")
    path: str | None = None
    line: int | None = None
    start_line: int | None = None
    suggestion: str | None = Field(
        default=None, description="Replacement code for the line range, if any."
    )
    verified: bool | None = None


class DismissedFinding(BaseModel):
    title: str
    reason: str


class TestRun(BaseModel):
    command: str
    where: str = ""
    result: str
    skipped_reason: str | None = None


class ReviewResult(BaseModel):
    """The structure every review skill returns (design 5.6)."""

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    dismissed: list[DismissedFinding] = Field(default_factory=list)
    tests_run: list[TestRun] = Field(default_factory=list)
    sections: dict[str, str] = Field(default_factory=dict)
    recommendation: str
    remaining_actions: list[str] = Field(default_factory=list)
    labels: list[str] = Field(
        default_factory=list,
        description="Labels from the list you were given that this PR should carry "
        "and does not have yet; empty when none clearly applies.",
    )


class FindingStatus(BaseModel):
    id: str
    title: str = ""
    status: Literal["resolved", "open", "partial"]
    fixed_in: str | None = None
    note: str = ""


class FollowupResult(BaseModel):
    statuses: list[FindingStatus] = Field(default_factory=list)
    new_findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    recommendation: str
    labels: list[str] = Field(
        default_factory=list,
        description="Labels from the list you were given that this PR should carry "
        "now and does not have yet; empty when none clearly applies.",
    )


class RenderedReview(BaseModel):
    body: str
    comments: list[InlineComment] = Field(default_factory=list)


def _finding_heading(f: Finding) -> str:
    return f"**{f.id} · {f.severity} · {f.category}: {f.title}**"


def _inline_body(f: Finding, own_format: bool = False) -> str:
    """An inline comment: the finding's heading and body, or, when the repository's
    skill sets the format, the body as the agent wrote it to that format."""
    parts = (
        [f.body.strip()] if own_format else [_finding_heading(f), "", f.body.strip()]
    )
    if f.suggestion:
        parts += ["", "```suggestion", f.suggestion.rstrip("\n"), "```"]
    return "\n".join(parts)


def render_review(
    result: ReviewResult,
    profile: RepoProfile,
    diff_paths: set[str],
    *,
    own_format: bool = False,
) -> RenderedReview:
    """Render a review into a main comment and inline comments.

    A finding goes inline when it names a path in the diff and a line; the rest go
    in the main comment. Sections come from the profile's template: built-in
    section names are filled from the result's fields, and any other section from
    ``result.sections``. With ``own_format`` (the repository's review skill sets
    the format) the main comment is ``result.sections`` as the agent returned them,
    in its order, when it returned any.
    """
    inline_findings = [
        f for f in result.findings if f.path in diff_paths and f.line is not None
    ][: profile.review.max_inline_comments]
    inline_ids = {f.id for f in inline_findings}
    comments = [
        InlineComment(
            path=f.path or "",
            line=f.line or 1,
            start_line=f.start_line
            if f.start_line and f.start_line < (f.line or 0)
            else None,
            body=_inline_body(f, own_format),
        )
        for f in inline_findings
    ]

    def findings_text() -> str:
        if not result.findings:
            return "- No issues found."
        lines = []
        for f in result.findings:
            where = f" (`{f.path}:{f.line}`)" if f.id in inline_ids else ""
            lines.append(f"- {_finding_heading(f)}{where}")
            if f.id not in inline_ids:
                lines.append(f"  {f.body.strip()}")
        return "\n".join(lines)

    def tests_text() -> str:
        if not result.tests_run:
            return "- No tests were run for this review."
        out = []
        for t in result.tests_run:
            where = f" on {t.where}" if t.where else ""
            if t.skipped_reason:
                out.append(f"- Skipped `{t.command}`{where}: {t.skipped_reason}")
            else:
                out.append(f"- `{t.command}`{where}: {t.result}")
        return "\n".join(out)

    def recommendation_text() -> str:
        lines = [f"**{result.recommendation}**"]
        lines += [f"- {a}" for a in result.remaining_actions]
        return "\n".join(lines)

    builtin = {
        "summary": lambda: result.summary.strip(),
        "findings": findings_text,
        "tests and docs": tests_text,
        "recommendation": recommendation_text,
    }
    if own_format and any(text.strip() for text in result.sections.values()):
        body = "\n\n".join(
            f"### {heading.strip()}\n\n{text.strip()}"
            for heading, text in result.sections.items()
            if text.strip()
        )
        return RenderedReview(body=body, comments=comments)
    blocks = []
    for section in profile.review.template:
        if section in result.sections:
            text = result.sections[section].strip()
        elif section.lower() in builtin:
            text = builtin[section.lower()]()
        else:
            text = "- Not assessed in this review."
        blocks.append(f"### {section}\n\n{text}")
    return RenderedReview(body="\n\n".join(blocks), comments=comments)


def missing_sections(result: ReviewResult, profile: RepoProfile) -> list[str]:
    """Template sections that neither the result nor a built-in field answers."""
    builtin = {"summary", "findings", "tests and docs", "recommendation"}
    return [
        s
        for s in profile.review.template
        if s not in result.sections and s.lower() not in builtin
    ]


def render_followup(
    result: FollowupResult,
    head_sha: str,
    previous_sha: str | None,
    profile: RepoProfile,
) -> RenderedReview:
    """Render a follow-up: a status per earlier finding, then new findings."""
    head7 = short_sha(head_sha)
    resolved = [s for s in result.statuses if s.status == "resolved"]
    if (
        result.statuses
        and len(resolved) == len(result.statuses)
        and not result.new_findings
    ):
        body = f"All earlier review comments are resolved as of `{head7}`."
        return RenderedReview(
            body=f"### Follow-up at `{head7}`\n\n{body}\n\n**{result.recommendation}**"
        )
    lines = [f"### Follow-up at `{head7}`", ""]
    if result.summary:
        lines += [result.summary.strip(), ""]
    for s in result.statuses:
        label = s.title or s.id
        if s.status == "resolved":
            lines.append(
                f"- ✅ Resolved in `{short_sha(s.fixed_in or head_sha)}`: {label}"
            )
        elif s.status == "partial":
            lines.append(f"- ◐ Partly addressed: {label}. {s.note}".rstrip())
        else:
            lines.append(f"- ⚠️ Still open: {label}. {s.note}".rstrip())
    comments: list[InlineComment] = []
    if result.new_findings:
        lines += ["", f"### New findings since `{short_sha(previous_sha)}`", ""]
        for f in result.new_findings:
            lines.append(f"- {_finding_heading(f)}")
            lines.append(f"  {f.body.strip()}")
            if f.path and f.line:
                comments.append(
                    InlineComment(path=f.path, line=f.line, body=_inline_body(f))
                )
    lines += ["", "### Recommendation", "", f"**{result.recommendation}**"]
    return RenderedReview(
        body="\n".join(lines).strip(),
        comments=comments[: profile.review.max_inline_comments],
    )
