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

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from .actions import ActionKind, ActionStatus, InlineComment
from .diffs import Ranges, fits, hunk_of
from .ids import same_commit, short_sha
from .markers import REVIEW_KINDS, parse_markers
from .profile import RepoProfile
from .records import ActionRecord
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
    or not). Only reviews and comments carrying a review marker count, so a review a
    person wrote by hand never does, even when the service acts as that person's
    account; a dismissed review with no body does not either. A review by anyone
    else counts too when its marker names one of ``own_actions``: a draft of this
    service that a maintainer posted as themselves. A marker alone never does, so
    nobody can fake a review.
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
        markers = [
            m for m in parse_markers(r.body, legacy_markers) if m.kind in REVIEW_KINDS
        ]
        if not markers:
            continue
        sha = next((m.sha for m in reversed(markers) if m.sha), r.commit_sha)
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


class QualityVerdict(BaseModel):
    """The reviewing agent's judgment of the PR's quality, following the repository's
    quality guide (``review.quality``). Internal: never posted."""

    score: int = Field(
        ge=0,
        le=100,
        description="0 to 100, as the quality guide you were given defines it.",
    )
    reason: str = Field(
        default="",
        description="What most decides the score, in plain words (Markdown).",
    )
    reasons: dict[str, str] = Field(
        default_factory=dict,
        description="The reason in each language you were asked for, keyed by its "
        "code: plain words for a maintainer new to the PR, no code or file names "
        "(Markdown).",
    )
    """The dashboard shows each person the reason in their language, else ``reason``."""
    checks: dict[str, bool | None] = Field(
        default_factory=dict,
        description="Each check you were asked for: true when the PR meets it, false "
        "when it does not, null when it does not apply.",
    )
    """The basics the agent checked before scoring, by area (``playbooks.prompts.
    QUALITY_CHECKS``), for reviewers to see at a glance."""

    @field_validator("checks", mode="before")
    @classmethod
    def _flags(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return {}
        return {str(k): v for k, v in value.items() if v is None or isinstance(v, bool)}

    @field_validator("score", mode="before")
    @classmethod
    def _round(cls, value: Any) -> Any:
        if isinstance(value, float):
            return round(value)
        return value

    @field_validator("reasons", mode="before")
    @classmethod
    def _texts(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return {}
        return {str(k): v.strip() for k, v in value.items() if isinstance(v, str)}

    @model_validator(mode="after")
    def _one_reason(self) -> QualityVerdict:
        if not self.reason and self.reasons:
            self.reason = self.reasons.get("en") or next(iter(self.reasons.values()))
        return self


def _lenient_quality(value: Any) -> Any:
    """A quality the agent got wrong is dropped rather than failing its review: a
    score outside 0 to 100 is clamped, and anything else unreadable becomes ``None``."""
    if value is None or isinstance(value, QualityVerdict):
        return value
    if isinstance(value, int | float):
        value = {"score": value}
    if not isinstance(value, dict):
        return None
    raw = value.get("score")
    if isinstance(raw, str):
        try:
            raw = float(raw.strip())
        except ValueError:
            return None
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return None
    return {
        "score": max(0, min(100, round(raw))),
        "reason": str(value.get("reason") or ""),
        "reasons": value.get("reasons") or {},
        "checks": value.get("checks") or {},
    }


_QUALITY_DESCRIPTION = (
    "Your judgment of the PR's quality, following the quality guide you were given. "
    "Internal: never mention it in the summary, the sections, or the findings."
)


class ReviewResult(BaseModel):
    """The structure every review skill returns (design 5.6)."""

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    dismissed: list[DismissedFinding] = Field(default_factory=list)
    tests_run: list[TestRun] = Field(
        default_factory=list,
        description="Tests a reproduction ran on a test machine, from its record; "
        "never tests you ran yourself on this machine.",
    )
    sections: dict[str, str] = Field(default_factory=dict)
    recommendation: str
    remaining_actions: list[str] = Field(default_factory=list)
    labels: list[str] = Field(
        default_factory=list,
        description="Labels from the list you were given that this PR should carry "
        "and does not have yet; empty when none clearly applies.",
    )
    quality: QualityVerdict | None = Field(
        default=None, description=_QUALITY_DESCRIPTION
    )

    _quality = field_validator("quality", mode="before")(_lenient_quality)


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
    quality: QualityVerdict | None = Field(
        default=None,
        description=_QUALITY_DESCRIPTION + " Judge the PR as it is at its head now.",
    )

    _quality = field_validator("quality", mode="before")(_lenient_quality)


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
    ranges: Ranges | None = None,
) -> RenderedReview:
    """Render a review into a main comment and inline comments.

    A finding goes inline when it names a path in the diff and a line that the
    diff's hunks show (``ranges``, from ``diffs.commentable``), since the forge
    refuses the whole review over a comment on any other line; a range that starts
    before its hunk is cut to the hunk. The rest go in the main comment with their
    location. Sections come from the profile's template: built-in
    section names are filled from the result's fields, and any other section from
    ``result.sections``. With ``own_format`` (the repository's review skill sets
    the format) the main comment is ``result.sections`` as the agent returned them,
    in its order, when it returned any, followed by the findings listed by id, so a
    section that refers to ``F1`` can be looked up.
    """

    def place(f: Finding) -> tuple[int, int | None] | None:
        """The lines an inline comment on ``f`` covers, or ``None`` for the body."""
        if f.path not in diff_paths or f.line is None:
            return None
        start = f.start_line if f.start_line and f.start_line < f.line else None
        if ranges is None or fits(ranges, f.path, f.line, start) is not False:
            return f.line, start
        hunk = hunk_of(ranges, f.path, f.line)
        if hunk is None or f.suggestion:  # a suggestion replaces exactly its lines
            return None
        return f.line, (hunk[0] if hunk[0] < f.line else None)

    placed = [(f, where) for f in result.findings if (where := place(f)) is not None]
    placed = placed[: profile.review.max_inline_comments]
    inline_ids = {f.id for f, _ in placed}
    comments = [
        InlineComment(
            path=f.path or "",
            line=line,
            start_line=start,
            body=_inline_body(f, own_format),
        )
        for f, (line, start) in placed
    ]

    def findings_text() -> str:
        if not result.findings:
            return "- No issues found."
        lines = []
        for f in result.findings:
            where = f" (`{f.path}:{f.line}`)" if f.path and f.line else ""
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
        headings = [
            (heading.strip().lstrip("#").strip(), text.strip())
            for heading, text in result.sections.items()
            if text.strip()
        ]
        listed = next(
            (s for s in profile.review.template if s.lower() == "findings"), "Findings"
        )
        if result.findings and listed.lower() not in {h.lower() for h, _ in headings}:
            headings.append((listed, findings_text()))
        body = "\n\n".join(f"### {heading}\n\n{text}" for heading, text in headings)
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


# ---------------------------------------------------------------------------
# Quality: the reviewing agent's judgment of the PR (design 5.6).
# ---------------------------------------------------------------------------

QualityBand = Literal["excellent", "good", "fair", "poor", "bad"]

QUALITY_BANDS: tuple[tuple[int, QualityBand], ...] = (
    (90, "excellent"),
    (75, "good"),
    (60, "fair"),
    (40, "poor"),
    (0, "bad"),
)
"""The lowest score of each band, best first; the default quality guide describes
the same bands to the agent."""

_COUNTED_DRAFTS = {
    ActionStatus.SHADOWED,
    ActionStatus.PROPOSED,
    ActionStatus.QUEUED,
    ActionStatus.INTENT,
    ActionStatus.SUPERSEDED,
    ActionStatus.FAILED,
}
"""Statuses of an unposted review that still state the service's latest
assessment. A draft a maintainer rejected (``denied``) does not."""


class QualityAssessment(QualityVerdict):
    """A quality an agent gave a review after it was written: for a review that had
    none, or when a maintainer asked for the PR to be scored again."""

    action_id: str
    """The ``review.post`` action it scores."""
    sha: str = ""
    at: datetime


class ReviewQuality(BaseModel):
    """A PR's quality as the agent judged it in its latest review that has one, or in
    a later assessment of that review. Internal: it never reaches a public action."""

    score: int = Field(ge=0, le=100)
    band: QualityBand
    reason: str = ""
    reasons: dict[str, str] = Field(default_factory=dict)
    """The reason by language code; ``reason`` where a language is missing."""
    checks: dict[str, bool | None] = Field(default_factory=dict)
    sha: str = ""
    """The head the judged review looked at."""
    kind: Literal["review", "followup"] = "review"
    posted: bool = True
    """Whether that review was posted, or is a draft or a shadow run."""
    action_id: str = ""
    rescored: bool = False
    """Whether the score comes from a later assessment, not from the review itself."""


def quality_band(score: int) -> QualityBand:
    """The band a score falls in."""
    for floor, band in QUALITY_BANDS:
        if score >= floor:
            return band
    return "bad"


class QuickQuality(QualityVerdict):
    """A quick score of a PR from its diff, by the same quality guide, for choosing
    what to review first. The quality its review judges (``ReviewQuality``) is the
    accurate one and takes its place once there is one. Internal."""

    sha: str = ""
    """The head it covers."""
    at: datetime
    lines: int | None = None
    """The PR's changed lines when it was scored, for ``barely_moved``."""
    paths: str | None = None
    """A digest of the PR's changed files when it was scored."""
    scored_sha: str | None = None
    """The head an agent looked at, when the score was carried over to ``sha`` from
    it (``barely_moved``)."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def band(self) -> QualityBand:
        return quality_band(self.score)


def paths_digest(paths: Iterable[str]) -> str:
    """A short digest of a set of file paths."""
    return hashlib.sha256("\n".join(sorted(set(paths))).encode()).hexdigest()[:16]


def barely_moved(quick: QuickQuality, lines: int, paths: Iterable[str]) -> bool:
    """Whether a PR whose head moved past ``quick`` still changes the same files by
    about as many lines (within ``max(10, 5%)``), so its quick score still stands: a
    quick score orders the review queue, and an agent call for each small push cost
    about 60,000 tokens."""
    if quick.lines is None or quick.paths is None:
        return False
    if quick.paths != paths_digest(paths):
        return False
    return abs(lines - quick.lines) <= max(10, quick.lines // 20)


def review_chain(records: Iterable[ActionRecord]) -> list[ActionRecord]:
    """A PR's reviews as follow-ups build on them, oldest first: the posted
    ``review.post`` actions, then the latest one when it is a newer draft or shadow
    run."""
    reviews = sorted(
        (
            r
            for r in records
            if r.action.kind == ActionKind.REVIEW_POST
            and (r.status == ActionStatus.EXECUTED or r.status in _COUNTED_DRAFTS)
        ),
        key=lambda r: r.action.created_at,
    )
    chain = [r for r in reviews if r.status == ActionStatus.EXECUTED]
    if reviews and reviews[-1].status != ActionStatus.EXECUTED:
        chain.append(reviews[-1])
    return chain


def review_quality(
    records: Iterable[ActionRecord], assessed: QualityAssessment | None = None
) -> ReviewQuality | None:
    """The quality of one PR from its ``review.post`` actions, in any order: the
    newest review in its chain with a quality, where ``assessed`` replaces the
    quality of the review it scores. ``None`` when no review has one."""
    for record in reversed(review_chain(records)):
        verdict: QualityVerdict | None
        rescored = assessed is not None and assessed.action_id == record.action.id
        if rescored:
            verdict = assessed
        else:
            raw = (record.action.payload.get("result") or {}).get("quality")
            try:
                verdict = QualityVerdict.model_validate(_lenient_quality(raw))
            except ValueError:
                verdict = None
        if verdict is None:
            continue
        return ReviewQuality(
            score=verdict.score,
            band=quality_band(verdict.score),
            reason=verdict.reason,
            reasons=verdict.reasons,
            checks=verdict.checks,
            sha=record.action.head_sha or "",
            kind="followup"
            if record.action.payload.get("kind") == "followup"
            else "review",
            posted=record.status == ActionStatus.EXECUTED,
            action_id=record.action.id,
            rescored=rescored,
        )
    return None
