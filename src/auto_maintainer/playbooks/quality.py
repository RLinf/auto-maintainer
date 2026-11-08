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
from datetime import timedelta

from ..domain.actions import ActionKind
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ports import Store
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, JobStatus
from ..domain.review import (
    QualityAssessment,
    QualityVerdict,
    QuickQuality,
    review_chain,
    review_quality,
)
from ..domain.snapshot import PrSnapshot, PrState
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import pr_block, quality_block, schema_hint, untrusted

QUICK_STATE = "quick_quality:{repo}#{number}"
"""The latest quick score of a PR, from its diff."""
QUICK_BATCH = 6
"""PRs the scan sends for a quick score per repository, once the last batch is done."""
QUICK_REFRESH = timedelta(hours=6)
"""A quick score at an older head is scored again once it is at least this old."""
QUALITY_STATE = "quality:{repo}#{number}"
"""The latest quality an agent gave a PR's review after it was written."""
QUALITY_TRIED = "quality_tried:{action_id}"
"""Set once the scan asked for a review's quality, so a failed run is not repeated."""
SCAN_BATCH = 3
"""Reviews the scan sends for scoring per repository and tick."""

PROMPT = """You judge the quality of PR #{number} in {repo} for its maintainers, inside an automated maintainer service. The service already reviewed it; that review is below as data.
- Read the code in the working directory (a checkout of the reviewed commit), the repository's docs and contributor guides, and the code around the change, so you judge how the PR fits this repository's design and style.
- Use the review's findings as evidence, and check the ones that decide the score against the code.
- Do not ask questions, post, push, or edit files. Return only the structured result."""


QUICK = """You give PR #{number} in {repo} a quick quality score, so its maintainers can choose which PRs to review first; a full review comes later and gives the accurate score.
- Read the diff, and only as much of the code around it and of the repository's docs as you need to judge how the change fits this repository's design and style. A few minutes, not more.
- Do not run tests, builds, or the PR's code, and do not write a review.
- Do not ask questions, post, push, or edit files. Return only the structured result."""


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
        prompt = "\n\n".join(
            part
            for part in [
                QUICK.format(number=pr.number, repo=pr.repo),
                quality_block(profile),
                f"Review lessons from this repository's maintainers:\n{lessons}"
                if lessons
                else "",
                pr_block(pr, diff, 60_000),
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
        score=verdict.score, reason=verdict.reason, sha=pr.head_sha, at=ctx.clock.now()
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
    quickly from its diff when it has no review or ``quick`` is asked for."""

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
        if not chain or event.payload.get("quick"):
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
    background (``quality.quick``, from the scan)."""

    name = "quick_quality"
    triggers = (EventKind.QUICK_QUALITY,)
    pool = "quality"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return profile is not None and profile.review.quick_quality

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "the PR is not open or in scope"})
        records = await ctx.store.list_actions(
            repo=pr.repo, number=pr.number, kinds=[ActionKind.REVIEW_POST], limit=500
        )
        if review_quality(records) is not None:
            return PlaybookResult(result={"skipped": "its review scored it"})
        quick = await quick_quality(ctx.store, pr.repo, pr.number)
        if quick is not None and quick.sha == pr.head_sha:
            return PlaybookResult(result={"skipped": "scored at this head already"})
        return _quick_result(await score_quickly(ctx, pr, profile))


class QualityScan(Playbook):
    """On the review scan's tick: asks for the quality of the latest review of each
    open PR that has none, ``SCAN_BATCH`` at a time and once per review; and, unless
    the repository turns it off, for a quick score of the PRs nobody reviewed,
    ``QUICK_BATCH`` at a time once the last batch is done, highest priority first:
    those never scored, then those scored at an older head ``QUICK_REFRESH`` ago."""

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
        unreviewed: list[PrSnapshot] = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            if not in_scope(pr, profile):
                continue
            chain = review_chain(by_pr.get(pr.number, []))
            if not chain:
                unreviewed.append(pr)
                continue
            if len(events) >= SCAN_BATCH:
                continue
            latest = chain[-1]
            assessed = await assessed_quality(ctx.store, pr.repo, pr.number)
            quality = review_quality(chain, assessed)
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
            await self._quick(ctx, profile, unreviewed)
            if profile.review.quick_quality
            else []
        )
        return PlaybookResult(
            events=events + quick,
            result={
                "requested": [e.number for e in events],
                "quick": [e.number for e in quick],
            },
        )

    async def _quick(
        self, ctx: PlaybookContext, profile: RepoProfile, prs: list[PrSnapshot]
    ) -> list[Event]:
        active = await ctx.store.list_jobs(
            [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING],
            repo=profile.repo,
            limit=500,
        )
        if any(job.playbook == "quick_quality" for job in active):
            return []
        now = ctx.clock.now()
        due: list[tuple[int, float, PrSnapshot]] = []
        for pr in prs:
            if pr.is_draft and not profile.scope.include_drafts:
                continue
            quick = await quick_quality(ctx.store, pr.repo, pr.number)
            if quick is not None and (
                quick.sha == pr.head_sha or now - quick.at < QUICK_REFRESH
            ):
                continue
            evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
            due.append(
                (
                    0 if quick is None else 1,
                    -(evaluation.total if evaluation else 0),
                    pr,
                )
            )
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
