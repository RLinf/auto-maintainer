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

"""What starts a review: manual requests (the default), the periodic scan, and events."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from auto_maintainer.domain.actions import ActionKind, ActionStatus, ProposalState
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.scoring import Evaluation
from auto_maintainer.domain.snapshot import Commit, PrFile
from auto_maintainer.playbooks.review_scan import scan_due
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness

REPO = "acme/widget"
REVIEW = {
    "summary": "Guards against empty batches.",
    "findings": [
        {
            "id": "F1",
            "severity": "minor",
            "category": "tests",
            "title": "Missing case",
            "body": "b",
            "path": "src/a.py",
            "line": 3,
        }
    ],
    "recommendation": "Ready",
    "quality": {"score": 84, "reason": "A small fix in the right place."},
}


def _pr(number: int, sha: str, **kw):
    return make_pr(
        number=number,
        sha=sha,
        files=[PrFile(path="src/a.py")],
        commits=[Commit(sha=sha, message="fix: a\n\nSigned-off-by: A <a@x>")],
        **kw,
    )


def _answers(h: Harness) -> None:
    h.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    h.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    h.runner.on("review", lambda t: REVIEW)
    h.runner.on("verify", lambda t: {"verdicts": [{"id": "F1", "confirmed": True}]})
    h.runner.on(
        "followup",
        lambda t: {
            "statuses": [{"id": "F1", "status": "resolved"}],
            "recommendation": "Ready",
        },
    )


def _request(number: int, by: str = "maria") -> Event:
    return Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=REPO,
        number=number,
        payload={"command": "review", "by": by, "source": "dashboard"},
    )


def _kinds(h: Harness, kind: str) -> list:
    return [t for t in h.runner.tasks if str(t.kind) == kind]


async def test_manual_is_the_default_and_drafts_go_to_the_requester(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, layer={"extends": ["dco"]})
    try:
        _answers(h)
        h.forge.put_pr(_pr(42, "a" * 40))
        await h.poll()
        assert not _kinds(h, "review"), "manual mode reviews nothing on its own"

        await h.emit(_request(42))
        [proposal] = await h.store.list_proposals(ProposalState.PENDING)
        record = await h.store.get_action(proposal.action_id)
        assert (
            record.action.kind == ActionKind.REVIEW_POST
            and proposal.requested_by == "maria"
        )
        assert record.decision.rule == "requested_review", (
            "a requested draft is a proposal even in shadow"
        )
        assert not h.forge.writes_of("create_review")

        await h.service.pipeline.approve(proposal.id, "maria")
        await h.settle()
        [posted] = h.forge.writes_of("create_review")
        assert "approved by maria" in posted["body"]

        pr = await h.forge.get_pr(REPO, 42)
        h.forge.put_pr(pr.model_copy(update={"head_sha": "b" * 40}))
        await h.poll()
        assert not _kinds(h, "followup"), "new commits wait for the reviewer to ask"
        await h.emit(_request(42))
        assert len(_kinds(h, "followup")) == 1 and len(_kinds(h, "review")) == 1
        drafts = [
            p
            for p in await h.store.list_proposals(ProposalState.PENDING)
            if p.requested_by == "maria"
        ]
        assert drafts, "the follow-up is a draft for the reviewer too"
    finally:
        await h.close()


async def test_up_to_date_requests_and_comment_requests(tmp_path: Path) -> None:
    from auto_maintainer.domain.snapshot import IssueComment

    h = await Harness.create(tmp_path, layer={})
    try:
        _answers(h)
        h.forge.put_pr(_pr(42, "a" * 40))
        await h.poll()
        h.forge.put_pr(
            (await h.forge.get_pr(REPO, 42)).model_copy(
                update={
                    "comments": [
                        IssueComment(id=7, author="outsider", body="/am review")
                    ]
                }
            )
        )
        await h.poll()
        assert not _kinds(h, "review"), (
            "a commenter without write access cannot request reviews"
        )
        h.forge.put_pr(
            (await h.forge.get_pr(REPO, 42)).model_copy(
                update={
                    "comments": [IssueComment(id=8, author="maria", body="/am review")]
                }
            )
        )
        await h.poll()
        assert len(_kinds(h, "review")) == 1
        [proposal] = await h.store.list_proposals(ProposalState.PENDING)
        assert proposal.requested_by == "maria"
    finally:
        await h.close()


async def test_dev_mode_keeps_requested_drafts_in_shadow(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, layer={}, dev_mode=True)
    try:
        _answers(h)
        h.forge.put_pr(_pr(42, "a" * 40))
        await h.poll()
        await h.emit(_request(42))
        assert not await h.store.list_proposals(ProposalState.PENDING)
        assert [
            r.status for r in await h.store.list_actions(kinds=[ActionKind.REVIEW_POST])
        ] == [ActionStatus.SHADOWED]
    finally:
        await h.close()


async def test_periodic_scan_reviews_a_bounded_batch_by_priority(
    tmp_path: Path,
) -> None:
    layer = {
        "review": {
            "trigger": "scan",
            "scan": {"schedule": "0 */6 * * *", "max_prs": 1},
            "quick_quality": False,
        }
    }
    h = await Harness.create(tmp_path, layer=layer)
    try:
        _answers(h)
        h.forge.put_pr(_pr(40, "a" * 40))
        h.forge.put_pr(_pr(41, "b" * 40))
        await h.poll()
        assert not _kinds(h, "review"), "scan mode waits for the schedule"
        await h.store.save_evaluation(Evaluation(repo=REPO, number=40, total=50.0))
        await h.store.save_evaluation(Evaluation(repo=REPO, number=41, total=80.0))

        tick = Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"})
        await h.emit(tick)
        assert [
            t.prompt.split("PR: #")[1].split(" ")[0] for t in _kinds(h, "review")
        ] == ["41"]
        assert not await h.store.list_proposals(ProposalState.PENDING), (
            "scan drafts follow policy (shadow here)"
        )

        await h.emit(tick.model_copy(update={"id": "evt_again"}))
        assert len(_kinds(h, "review")) == 1, "not due again yet"

        h.clock.advance(hours=6)
        await h.emit(tick.model_copy(update={"id": "evt_later"}))
        assert [
            t.prompt.split("PR: #")[1].split(" ")[0] for t in _kinds(h, "review")
        ] == ["41", "40"]
    finally:
        await h.close()


def test_scan_due() -> None:
    last = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)
    assert scan_due("0 */6 * * *", None, last)
    assert not scan_due("0 */6 * * *", last, datetime(2026, 1, 5, 11, 59, tzinfo=UTC))
    assert scan_due("0 */6 * * *", last, datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
