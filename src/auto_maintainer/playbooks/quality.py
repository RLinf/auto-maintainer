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

"""A PR's quality: a quick score at a glance, and the accurate one after review
(design 5.6).

The reviewing agent scores a PR in its review: that is the accurate score. Before a
review, ``QuickQualityPlaybook`` gives each open PR a quick score from its diff by the
same quality guide, so reviewers see at a glance which PRs to take first; it runs in
the ``quality`` pool, apart from the reviews people wait for, and ``QualityScan``
feeds it a few PRs at a time, highest priority first (``review.quick_quality`` turns
it off). ``QualityPlaybook`` scores on a maintainer's request (Score quality): a
reviewed PR's latest review again, or an unreviewed PR quickly. The scan also asks it
to score the latest review of a PR that has none (one written before reviews scored
quality, or whose agent left it out), once per review. Scores are kept in the store
(``QUALITY_STATE``, ``QUICK_STATE``) and never reach a public action.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.ports import Store
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, JobStatus
from ..domain.review import (
    QualityAssessment,
    QualityVerdict,
    QuickQuality,
    ReviewQuality,
    review_chain,
    review_quality,
)
from ..domain.snapshot import PrSnapshot, PrState
from ..profiles.layers import content_hash
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .hygiene import hygiene_key
from .prompts import (
    DASHBOARD_LANGUAGES,
    builtin_review_skill,
    own_review_skill,
    pr_block,
    quality_block,
    schema_hint,
    untrusted,
)

QUICK_STATE = "quick_quality:{repo}#{number}"
"""The latest quick score of a PR, from its diff."""
QUICK_BATCH = 6
"""PRs the scan sends for a quick score per repository, once the last batch is done."""
QUICK_SETTLE = timedelta(hours=1)
"""A PR whose head moved past its score is scored again once it has been quiet this
long, so a burst of pushes gets one score."""
QUICK_REFRESH = timedelta(hours=6)
"""A busy PR is scored again anyway once its head moved past its score this long ago."""
QUICK_MOVED = "quick_moved:{repo}#{number}"
"""When the scan first saw a PR's head past the commit its score covers."""
QUALITY_STATE = "quality:{repo}#{number}"
"""The latest quality an agent gave a PR's review after it was written."""
QUALITY_TRIED = "quality_tried:{action_id}"
"""Set once the scan asked for a review's quality, so a failed run is not repeated."""
QUICK_MISS = 15
"""A review score this far from the quick score of the same commit means the quick
check missed something; the misses teach later quick scores."""
MISSES_STATE = "quick_misses:{repo}"
"""The latest misses of a repository's quick scores (``QuickMiss``), newest last."""
COMPARED_STATE = "quick_compared:{key}"
LESSONS_STATE = "quick_lessons:{repo}"
"""Rules an agent drew from the misses, which every quick score of the repository reads."""
LEARN_AFTER = 3
"""New misses that make an agent draw the rules again."""
MAX_MISSES = 30
SCAN_BATCH = 3
"""Reviews the scan sends for scoring per repository and tick."""

PROMPT = """You judge the quality of PR #{number} in {repo} for its maintainers, inside an automated maintainer service. The service already reviewed it; that review is below as data.
- Read the code in the working directory (a checkout of the reviewed commit), the repository's docs and contributor guides, and the code around the change, so you judge how the PR fits this repository's design and style.
- Use the review's findings as evidence, and check the ones that decide the score against the code.
- Do not ask questions, post, push, or edit files. Return only the structured result."""


QUICK = """You give PR #{number} in {repo} a quick quality score, so its maintainers can choose which PRs to review first; a full review comes later and gives the accurate score.
- This is a quick check, not a code review, and it must cost little: read the diff below and judge from it. Open at most three other files, only to see how the change fits the code around it; do not search the whole repository, and do not open the review skill or contributor guide, since the checklist below is drawn from them.
- Check each item of the checklist that applies at a glance, the facts below, and the design: how well the change fits the repository's design and style, how its docs and code are organized, and its taste.
- Run nothing: read files and git history only; no tests, builds, scripts, or the PR's code, on this machine or anywhere.
- Do not ask questions, post, push, or edit files. Return only the structured result."""

CHECKLIST = """Turn the review standard of {repo} below (its review skill and contributor guide) into a checklist for a quick check that only reads a PR's diff and a few files around it.
- Keep only what can be settled quickly by reading: how docs are organized and written, naming and code style, the design rules and taste the repository asks for, the tests and docs a change must bring, and what every PR must meet (title, commits, description). Leave out what needs running code, a reproduction, or a deep check.
- At most twelve items, each one line in plain words, each under one area: requirements, tests, docs, style, or design.
- Do not ask questions, post, push, edit files, or run anything. Return only the structured result."""

GUIDE_FILES = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)
"""Where repositories conventionally keep what a contributor must follow."""

CHECKLIST_STATE = "quick_checklist:{repo}"
QUICK_DIFF_LIMIT = 24_000
"""Characters of diff a quick score reads; a review reads up to 120,000."""
QUICK_FILE_LIMIT = 6_000
"""Characters of one file's diff a quick score reads."""
_SKIPPED_DIFFS = (
    ".lock",
    "-lock.json",
    "lock.yaml",
    ".min.js",
    ".svg",
    ".png",
    ".jpg",
    ".ipynb",
    ".csv",
    ".pkl",
    ".bin",
)
"""Files whose diff tells a quick check nothing: lock files, minified and binary
files, notebooks, and data. They stay in the file list."""


class ChecklistItem(BaseModel):
    area: str = Field(description="requirements, tests, docs, style, or design")
    check: str = Field(description="One line in plain words.")


class QuickChecklist(BaseModel):
    items: list[ChecklistItem] = Field(default_factory=list, max_length=12)


class QuickMiss(BaseModel):
    """A PR whose review scored it far from its quick score of the same commit."""

    number: int
    quick: int
    review: int
    quick_reason: str = ""
    review_reason: str = ""
    checks: dict[str, bool | None] = Field(default_factory=dict)
    at: str = ""


class QuickLessons(BaseModel):
    rules: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="At most six rules, each one line in plain words.",
    )


LEARN = """Quick quality scores of {repo}'s PRs are checked against the full review that follows them. Below are recent PRs where the two disagreed by {gap} points or more, with what the quick check and the review each said, the current rules for quick checks, and the repository's checklist.
- Write the rules a quick check of this repository should follow so it would have scored these PRs closer to their reviews: what to look for in a diff, and how much it weighs. At most six, each one line in plain words, about kinds of changes, not about these PRs.
- Keep the current rules that still hold, change those the misses contradict, and drop the rest.
- Do not ask questions, post, push, edit files, or run anything. Return only the structured result."""


async def quick_misses(store: Store, repo: str) -> list[QuickMiss]:
    raw = await store.get_state(MISSES_STATE.format(repo=repo))
    items = raw if isinstance(raw, list) else []
    out = []
    for item in items:
        try:
            out.append(QuickMiss.model_validate(item))
        except ValueError:
            continue
    return out


async def quick_lessons(store: Store, repo: str) -> dict:
    """The rules drawn from the misses: ``{"rules": [...], "at": ..., "misses": n}``,
    where ``misses`` counts the misses they were drawn from."""
    raw = await store.get_state(LESSONS_STATE.format(repo=repo))
    return raw if isinstance(raw, dict) else {}


def lessons_block(lessons: dict, misses: list[QuickMiss]) -> str:
    """What reviews taught the quick check: its learned rules, and the latest misses."""
    parts = []
    rules = [r for r in lessons.get("rules") or [] if isinstance(r, str)]
    if rules:
        parts.append(
            "Rules this repository's reviews taught quick checks (learned from quick "
            "scores a review corrected); follow them:\n"
            + "\n".join(f"- {r}" for r in rules)
        )
    if misses:
        parts.append(
            "Recent quick scores a review corrected; do not repeat these misses:\n"
            + untrusted(
                "calibration.misses",
                "\n".join(
                    f"#{m.number}: quick {m.quick}, after review {m.review}. The quick "
                    f"check said: {m.quick_reason[:200]} The review found: "
                    f"{m.review_reason[:240]}"
                    for m in misses[-4:]
                ),
                6_000,
            )
        )
    return "\n\n".join(parts)


def condensed_diff(diff: str) -> str:
    """The part of ``diff`` a quick score reads: each file's diff up to
    ``QUICK_FILE_LIMIT``, source files before tests, without lock, binary, and data
    files, up to ``QUICK_DIFF_LIMIT`` in all; what is left out is named."""
    blocks = [b for b in diff.split("diff --git ") if b.strip()]
    kept: list[tuple[int, str, str]] = []
    left: list[str] = []
    for block in blocks:
        name = block.split("\n", 1)[0].split(" b/")[-1].strip()
        if name.lower().endswith(_SKIPPED_DIFFS):
            left.append(name)
            continue
        rank = 1 if "test" in name.lower() else 0
        text = "diff --git " + block
        if len(text) > QUICK_FILE_LIMIT:
            text = (
                text[:QUICK_FILE_LIMIT]
                + "\n... (the rest of this file's diff is left out)\n"
            )
        kept.append((rank, name, text))
    kept.sort(key=lambda k: k[0])
    out: list[str] = []
    size = 0
    for _, name, text in kept:
        if size + len(text) > QUICK_DIFF_LIMIT:
            left.append(name)
            continue
        out.append(text)
        size += len(text)
    if left:
        out.append("Left out of this diff: " + ", ".join(left[:40]))
    return "".join(out)


async def quick_checklist(
    ctx: PlaybookContext, profile: RepoProfile, workdir: Path
) -> list[ChecklistItem]:
    """What the repository's review standard asks that a quick check can settle by
    reading: drawn once by an agent from its review skill and contributor guide, and
    kept until they change, so each quick score reads a dozen lines, not the skill."""
    skill = own_review_skill(profile, workdir)
    path = workdir / skill if skill else None
    if path is None:
        builtin = builtin_review_skill(profile, ctx.service.skills_dir)
        path = Path(builtin) if builtin else None
    sources: dict[str, str] = {}
    for name, file in [("review skill", path)] + [
        (g, workdir / g) for g in GUIDE_FILES
    ]:
        if file is not None and file.is_file():
            sources[name] = file.read_text(encoding="utf-8", errors="replace")[:20_000]
    if not sources:
        return []
    digest = content_hash(sources)
    key = CHECKLIST_STATE.format(repo=profile.repo)
    cached = await ctx.store.get_state(key)
    if isinstance(cached, dict) and cached.get("digest") == digest:
        return [ChecklistItem.model_validate(i) for i in cached.get("items") or []]
    prompt = "\n\n".join(
        [
            CHECKLIST.format(repo=profile.repo),
            *(
                untrusted(f"standard.{name}", text, 20_000)
                for name, text in sources.items()
            ),
            schema_hint(QuickChecklist.model_json_schema()),
        ]
    )
    answer = await ctx.ask(
        AgentTask(
            kind=TaskKind.EVALUATE,
            prompt=prompt,
            workdir=workdir,
            mode="read_only",
            run_code="none",
            budget="quick",
            output_schema=QuickChecklist.model_json_schema(),
            repo=profile.repo,
        ),
        QuickChecklist,
    )
    if not answer.ok or answer.structured is None:
        return []
    items = QuickChecklist.model_validate(answer.structured).items[:12]
    await ctx.store.set_state(
        key, {"digest": digest, "items": [i.model_dump() for i in items]}
    )
    return items


def checklist_block(items: list[ChecklistItem]) -> str:
    if not items:
        return ""
    return (
        "The repository's checklist for a quick check (from its review standard):\n"
        + "\n".join(f"- [{i.area}] {i.check}" for i in items)
    )


def quick_facts(pr: PrSnapshot, hygiene: dict | None) -> str:
    """What the service knows about ``pr`` that bears on its basics: its size, whether
    it touches tests and docs, its CI, and the repository's hygiene checks on it."""
    adds = sum(f.additions for f in pr.files)
    dels = sum(f.deletions for f in pr.files)
    paths = [f.path.lower() for f in pr.files]
    tests = [p for p in paths if "test" in p]
    docs = [p for p in paths if p.endswith((".md", ".rst")) or "docs/" in p]
    lines = [
        f"Size: {len(pr.files)} files, +{adds}/-{dels}, {len(pr.commits)} commits.",
        f"Files that look like tests: {len(tests)}. Files that look like docs: {len(docs)}.",
    ]
    failing = [c.name for c in pr.checks if (c.conclusion or "").lower() == "failure"]
    pending = [c.name for c in pr.checks if not c.conclusion]
    lines.append(
        f"CI: {len(pr.checks)} checks"
        + (f"; failing: {', '.join(failing[:10])}" if failing else "")
        + (f"; still running: {len(pending)}" if pending else "")
        + "."
        if pr.checks
        else "CI: no checks ran on this head."
    )
    results = (hygiene or {}).get("results") or []
    if results and (hygiene or {}).get("sha") == pr.head_sha:
        failed = [
            r.get("summary") or r.get("id")
            for r in results
            if not r.get("passed") and not r.get("skipped")
        ]
        lines.append(
            "The repository's hygiene checks at this head: "
            + (
                "all passed."
                if not failed
                else "failed: " + "; ".join(str(f) for f in failed[:10]) + "."
            )
        )
    return "Facts the service collected:\n" + "\n".join(f"- {line}" for line in lines)


async def calibration_block(ctx: PlaybookContext, repo: str) -> str:
    """Reference points for the score: PRs of ``repo`` a full review scored, from low
    to high, and how the quick scores given so far are spread."""
    by_pr: dict[int, list[ActionRecord]] = {}
    for record in await ctx.store.list_actions(
        repo=repo, kinds=[ActionKind.REVIEW_POST], limit=2000
    ):
        if record.action.number is not None:
            by_pr.setdefault(record.action.number, []).append(record)
    reviewed = []
    for number, records in by_pr.items():
        quality = review_quality(
            records, await assessed_quality(ctx.store, repo, number)
        )
        if quality is not None:
            reviewed.append((quality.score, number, quality))
    reviewed.sort()
    if len(reviewed) > 6:
        step = (len(reviewed) - 1) / 5
        reviewed = [reviewed[round(i * step)] for i in range(6)]
    quick = []
    for pr in await ctx.store.list_snapshots(repo, open_only=True):
        q = await quick_quality(ctx.store, repo, pr.number)
        if q is not None:
            quick.append(q.score)
    parts = []
    if reviewed:
        parts.append(
            "For calibration, PRs of this repository a full review scored:\n"
            + untrusted(
                "calibration.reviewed",
                "\n".join(
                    f"#{n}: {q.score} ({q.band}). {(q.reasons.get('en') or q.reason)[:240]}"
                    for _, n, q in reviewed
                ),
                6_000,
            )
        )
    if len(quick) >= 5:
        quick.sort()
        high = sum(1 for q in quick if q >= 90)
        parts.append(
            f"Quick scores given so far in this repository: {len(quick)}, from "
            f"{quick[0]} to {quick[-1]}, median {quick[len(quick) // 2]}, {high} at 90 "
            "or above. Score this PR on the guide's scale against these, so the scores "
            "tell reviewers which PRs are better."
        )
    return "\n\n".join(parts)


async def quick_quality(store: Store, repo: str, number: int) -> QuickQuality | None:
    """The latest quick score of ``repo#number``, if any."""
    raw = await store.get_state(QUICK_STATE.format(repo=repo, number=number))
    if not raw:
        return None
    try:
        return QuickQuality.model_validate(
            json.loads(raw) if isinstance(raw, str) else raw
        )
    except ValueError:
        return None


async def score_quickly(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile
) -> QuickQuality:
    """Have an agent score ``pr`` from its diff at its head, and keep the score."""
    ctx.trusted_input = await ctx.author_is_team(pr)
    info = await ctx.forge.get_repo(pr.repo)
    workdir = await ctx.workspace.prepare(
        pr.repo, pr.number, pr.head_sha, info.default_branch
    )
    try:
        diff = await ctx.workspace.diff(
            workdir, f"origin/{info.default_branch}", pr.head_sha
        )
        lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
        checklist = await quick_checklist(ctx, profile, workdir)
        prompt = "\n\n".join(
            part
            for part in [
                QUICK.format(number=pr.number, repo=pr.repo),
                quality_block(profile),
                checklist_block(checklist),
                lessons_block(
                    await quick_lessons(ctx.store, pr.repo),
                    await quick_misses(ctx.store, pr.repo),
                ),
                f"Review lessons from this repository's maintainers:\n{lessons[:3000]}"
                if lessons
                else "",
                quick_facts(
                    pr, await ctx.store.get_state(hygiene_key(pr.repo, pr.number))
                ),
                await calibration_block(ctx, pr.repo),
                pr_block(pr, condensed_diff(diff), QUICK_DIFF_LIMIT + 2_000),
                schema_hint(QualityVerdict.model_json_schema()),
            ]
            if part
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.EVALUATE,
                prompt=prompt,
                workdir=workdir,
                mode="read_only",
                run_code="none",
                budget="quick",
                output_schema=QualityVerdict.model_json_schema(),
                repo=pr.repo,
            ),
            QualityVerdict,
        )
    finally:
        await ctx.workspace.release(workdir)
    if not answer.ok or answer.structured is None:
        raise RuntimeError(f"the quality agent failed: {answer.error}")
    verdict = QualityVerdict.model_validate(answer.structured)
    quick = QuickQuality(
        score=verdict.score,
        reason=verdict.reason,
        reasons=verdict.reasons,
        checks=verdict.checks,
        sha=pr.head_sha,
        at=ctx.clock.now(),
    )
    await ctx.store.set_state(
        QUICK_STATE.format(repo=pr.repo, number=pr.number), quick.model_dump_json()
    )
    return quick


def _quick_result(quick: QuickQuality) -> PlaybookResult:
    return PlaybookResult(
        result={
            "quick": True,
            "score": quick.score,
            "band": quick.band,
            "reason": quick.reason,
        }
    )


async def assessed_quality(
    store: Store, repo: str, number: int
) -> QualityAssessment | None:
    """The quality an agent gave ``repo#number``'s review after it was written, if any."""
    raw = await store.get_state(QUALITY_STATE.format(repo=repo, number=number))
    if not raw:
        return None
    try:
        return QualityAssessment.model_validate(
            json.loads(raw) if isinstance(raw, str) else raw
        )
    except ValueError:
        return None


class QualityPlaybook(Playbook):
    """Scores one PR on request (``quality.requested``): its latest review again, or
    quickly from its diff at its head when it has no review, its head moved past the
    latest review, or ``quick`` is asked for."""

    name = "quality"
    triggers = (EventKind.QUALITY_REQUESTED,)
    pool = "agent"

    def key(self, event: Event) -> str:
        return f"quality:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        by = event.payload.get("by")
        if by:
            ctx.requested_by = str(by)
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        chain = review_chain(
            await ctx.store.list_actions(
                repo=pr.repo,
                number=pr.number,
                kinds=[ActionKind.REVIEW_POST],
                limit=500,
            )
        )
        if (
            not chain
            or event.payload.get("quick")
            or (
                event.payload.get("by")
                and not same_commit(chain[-1].action.head_sha, pr.head_sha)
            )
        ):
            return _quick_result(await score_quickly(ctx, pr, profile))
        review = chain[-1]
        head = review.action.head_sha or pr.head_sha
        ctx.trusted_input = await ctx.author_is_team(pr)
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, head, info.default_branch
        )
        try:
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", head
            )
            lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
            prompt = "\n\n".join(
                part
                for part in [
                    PROMPT.format(number=pr.number, repo=pr.repo),
                    quality_block(profile),
                    f"Review lessons from this repository's maintainers:\n{lessons}"
                    if lessons
                    else "",
                    untrusted(
                        "review.structured",
                        json.dumps(_review_data(review), ensure_ascii=False, indent=1),
                        40_000,
                    ),
                    pr_block(pr, diff),
                    schema_hint(QualityVerdict.model_json_schema()),
                ]
                if part
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.REVIEW,
                    prompt=prompt,
                    workdir=workdir,
                    mode="read_only",
                    output_schema=QualityVerdict.model_json_schema(),
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                ),
                QualityVerdict,
            )
        finally:
            await ctx.workspace.release(workdir)
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"the quality agent failed: {answer.error}")
        verdict = QualityVerdict.model_validate(answer.structured)
        assessment = QualityAssessment(
            score=verdict.score,
            reason=verdict.reason,
            reasons=verdict.reasons,
            checks=verdict.checks,
            action_id=review.action.id,
            sha=head,
            at=ctx.clock.now(),
        )
        await ctx.store.set_state(
            QUALITY_STATE.format(repo=pr.repo, number=pr.number),
            assessment.model_dump_json(),
        )
        quality = review_quality(chain, assessment)
        return PlaybookResult(
            result={
                "score": verdict.score,
                "band": quality.band if quality else None,
                "reason": verdict.reason,
                "review": review.action.id,
            }
        )


def _review_data(review: ActionRecord) -> dict[str, object]:
    """What the scoring agent reads of the review: its structured result without any
    earlier score, so it judges afresh."""
    result = dict(review.action.payload.get("result") or {})
    result.pop("quality", None)
    return {"kind": review.action.payload.get("kind") or "review", **result}


class QuickQualityPlaybook(Playbook):
    """Gives one PR nobody reviewed yet a quick score from its diff, in the
    background (``quality.quick``): from the scan, or for a person who asked for many
    PRs at once (``by``), which scores it again even at the same head."""

    name = "quick_quality"
    triggers = (EventKind.QUICK_QUALITY,)
    pool = "quality"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        asked = bool(event.payload.get("by"))
        return profile is not None and (asked or profile.review.quick_quality)

    def key(self, event: Event) -> str:
        """One quick score per head, unless a person asks for it again."""
        base = super().key(event)
        return f"{base}:{event.id}" if event.payload.get("by") else base

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "the PR is not open or in scope"})
        records = await ctx.store.list_actions(
            repo=pr.repo, number=pr.number, kinds=[ActionKind.REVIEW_POST], limit=500
        )
        reviewed = review_quality(
            records, await assessed_quality(ctx.store, pr.repo, pr.number)
        )
        if reviewed is not None and same_commit(reviewed.sha, pr.head_sha):
            return PlaybookResult(result={"skipped": "its review scored this head"})
        by = event.payload.get("by")
        if by:
            ctx.requested_by = str(by)
        quick = await quick_quality(ctx.store, pr.repo, pr.number)
        if not by and quick is not None and quick.sha == pr.head_sha:
            return PlaybookResult(result={"skipped": "scored at this head already"})
        return _quick_result(await score_quickly(ctx, pr, profile))


class QuickLearnPlaybook(Playbook):
    """Draws the rules quick checks of a repository follow from the quick scores its
    reviews corrected (``quality.learn``, from the scan), in the background."""

    name = "quick_learn"
    triggers = (EventKind.QUICK_LEARN,)
    pool = "quality"

    def key(self, event: Event) -> str:
        return f"quick_learn:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        misses = await quick_misses(ctx.store, profile.repo)
        if not misses:
            return PlaybookResult(result={"skipped": "no misses to learn from"})
        current = await quick_lessons(ctx.store, profile.repo)
        cached = await ctx.store.get_state(CHECKLIST_STATE.format(repo=profile.repo))
        checklist = [
            ChecklistItem.model_validate(i)
            for i in (cached.get("items") if isinstance(cached, dict) else None) or []
        ]
        rows = "\n".join(
            f"#{m.number}: quick {m.quick}, after review {m.review}. Checks the quick "
            f"check marked: {m.checks}. The quick check said: {m.quick_reason[:400]} "
            f"The review found: {m.review_reason[:400]}"
            for m in misses
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.EVALUATE,
                prompt="\n\n".join(
                    part
                    for part in [
                        LEARN.format(repo=profile.repo, gap=QUICK_MISS),
                        "Current rules:\n"
                        + "\n".join(f"- {r}" for r in current.get("rules") or [])
                        if current.get("rules")
                        else "There are no rules yet.",
                        checklist_block(checklist),
                        untrusted("quick.misses", rows, 20_000),
                        schema_hint(QuickLessons.model_json_schema()),
                    ]
                    if part
                ),
                workdir=ctx.scratch(),
                mode="read_only",
                run_code="none",
                budget="quick",
                output_schema=QuickLessons.model_json_schema(),
                repo=profile.repo,
            ),
            QuickLessons,
        )
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"the learning agent failed: {answer.error}")
        rules = [
            r.strip() for r in QuickLessons.model_validate(answer.structured).rules
        ][:6]
        await ctx.store.set_state(
            LESSONS_STATE.format(repo=profile.repo),
            {
                "rules": rules,
                "at": ctx.clock.now().isoformat(),
                "misses": int(event.payload.get("misses") or len(misses)),
            },
        )
        return PlaybookResult(result={"rules": rules})


class QualityScan(Playbook):
    """On the review scan's tick: asks for the quality of the latest review of each
    open PR that has none, ``SCAN_BATCH`` at a time and once per review; and, unless
    the repository turns it off, for a quick score of each PR whose head no score
    covers (nobody reviewed it, or new commits came after its review),
    ``QUICK_BATCH`` at a time once the last batch is done, highest priority first:
    those never scored, then those whose head moved once they have been quiet for
    ``QUICK_SETTLE`` (or their score is ``QUICK_REFRESH`` old), then those without a
    reason in every dashboard language or without checks."""

    name = "quality_scan"
    triggers = ()
    tick_names = ("review_scan",)

    def key(self, event: Event) -> str:
        return f"quality_scan:{event.repo}:{event.id}"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return profile is not None

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        by_pr: dict[int, list[ActionRecord]] = {}
        for record in await ctx.store.list_actions(
            repo=profile.repo, kinds=[ActionKind.REVIEW_POST], limit=5000
        ):
            if record.action.number is not None:
                by_pr.setdefault(record.action.number, []).append(record)
        events: list[Event] = []
        misses: list[QuickMiss] = []
        unscored: list[tuple[PrSnapshot, str | None]] = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            if not in_scope(pr, profile):
                continue
            chain = review_chain(by_pr.get(pr.number, []))
            if not chain:
                unscored.append((pr, None))
                continue
            latest = chain[-1]
            assessed = await assessed_quality(ctx.store, pr.repo, pr.number)
            quality = review_quality(chain, assessed)
            if quality is not None:
                missed = await self._compare(ctx, pr, quality)
                if missed is not None:
                    misses.append(missed)
            if quality is None or not same_commit(quality.sha, pr.head_sha):
                # New commits since the review: the new head gets a quick score.
                unscored.append((pr, quality.sha if quality else None))
            if len(events) >= SCAN_BATCH:
                continue
            if quality is not None and quality.action_id == latest.action.id:
                continue
            tried = QUALITY_TRIED.format(action_id=latest.action.id)
            if await ctx.store.get_state(tried):
                continue
            await ctx.store.set_state(tried, ctx.clock.now().isoformat())
            events.append(
                Event(
                    kind=EventKind.QUALITY_REQUESTED,
                    repo=pr.repo,
                    number=pr.number,
                    sha=pr.head_sha,
                    source="scan",
                )
            )
        quick = (
            await self._quick(ctx, profile, unscored)
            if profile.review.quick_quality
            else []
        )
        learn = await self._remember(ctx, profile.repo, misses)
        return PlaybookResult(
            events=events + quick + learn,
            result={
                "requested": [e.number for e in events],
                "quick": [e.number for e in quick],
                "misses": [m.number for m in misses],
            },
        )

    async def _compare(
        self, ctx: PlaybookContext, pr: PrSnapshot, quality: ReviewQuality
    ) -> QuickMiss | None:
        """Compare a review's score with the quick score of the same commit, once per
        review score; the miss, when they are ``QUICK_MISS`` apart."""
        quick = await quick_quality(ctx.store, pr.repo, pr.number)
        if quick is None or not same_commit(quick.sha, quality.sha):
            return None
        key = COMPARED_STATE.format(
            key=f"{quality.action_id}:{quality.score}:{quick.at.isoformat()}"
        )
        if await ctx.store.get_state(key):
            return None
        await ctx.store.set_state(key, ctx.clock.now().isoformat())
        if abs(quick.score - quality.score) < QUICK_MISS:
            return None
        return QuickMiss(
            number=pr.number,
            quick=quick.score,
            review=quality.score,
            quick_reason=quick.reasons.get("en") or quick.reason,
            review_reason=quality.reasons.get("en") or quality.reason,
            checks=quick.checks,
            at=ctx.clock.now().isoformat(),
        )

    async def _remember(
        self, ctx: PlaybookContext, repo: str, misses: list[QuickMiss]
    ) -> list[Event]:
        """Keep the new misses; once ``LEARN_AFTER`` of them came since the rules were
        last drawn, ask for the rules again."""
        known = await quick_misses(ctx.store, repo)
        if misses:
            known = (known + misses)[-MAX_MISSES:]
            await ctx.store.set_state(
                MISSES_STATE.format(repo=repo), [m.model_dump() for m in known]
            )
        total = int(
            await ctx.store.get_state(f"{MISSES_STATE.format(repo=repo)}:n") or 0
        )
        total += len(misses)
        if misses:
            await ctx.store.set_state(f"{MISSES_STATE.format(repo=repo)}:n", total)
        learned = int((await quick_lessons(ctx.store, repo)).get("misses") or 0)
        if total - learned < LEARN_AFTER:
            return []
        active = await ctx.store.list_jobs(
            [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING],
            repo=repo,
            limit=500,
        )
        if any(job.playbook == "quick_learn" for job in active):
            return []
        return [
            Event(
                kind=EventKind.QUICK_LEARN,
                repo=repo,
                payload={"misses": total},
                source="scan",
            )
        ]

    async def _quick(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        prs: list[tuple[PrSnapshot, str | None]],
    ) -> list[Event]:
        """``prs`` with the commit their review's score covers, when they have one."""
        active = await ctx.store.list_jobs(
            [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING],
            repo=profile.repo,
            limit=500,
        )
        if any(job.playbook == "quick_quality" for job in active):
            return []
        now = ctx.clock.now()
        due: list[tuple[int, float, PrSnapshot]] = []
        for pr, reviewed in prs:
            if pr.is_draft and not profile.scope.include_drafts:
                continue
            quick = await quick_quality(ctx.store, pr.repo, pr.number)
            complete = quick is not None and (
                set(DASHBOARD_LANGUAGES) <= set(quick.reasons) and bool(quick.checks)
            )
            scored = quick.sha if quick is not None else reviewed
            if quick is not None and same_commit(quick.sha, pr.head_sha):
                if complete:
                    continue
                rank = 2
            elif scored is not None:
                if not await self._settled(ctx, pr, scored, now):
                    continue
                rank = 1
            else:
                rank = 0
            evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
            due.append((rank, -(evaluation.total if evaluation else 0), pr))
        due.sort(key=lambda d: (d[0], d[1], -d[2].number))
        return [
            Event(
                kind=EventKind.QUICK_QUALITY,
                repo=pr.repo,
                number=pr.number,
                sha=pr.head_sha,
                source="scan",
            )
            for _, _, pr in due[:QUICK_BATCH]
        ]

    async def _settled(
        self, ctx: PlaybookContext, pr: PrSnapshot, scored: str, now: datetime
    ) -> bool:
        """Whether ``pr``, whose head moved past ``scored``, is due a new quick score:
        quiet for ``QUICK_SETTLE``, or moved past it ``QUICK_REFRESH`` ago."""
        key = QUICK_MOVED.format(repo=pr.repo, number=pr.number)
        seen = await ctx.store.get_state(key)
        if not isinstance(seen, dict) or seen.get("scored") != scored:
            seen = {"scored": scored, "at": now.isoformat()}
            await ctx.store.set_state(key, seen)
        moved = datetime.fromisoformat(seen["at"])
        quiet = now - (pr.updated_at or moved) >= QUICK_SETTLE
        return quiet or now - moved >= QUICK_REFRESH
