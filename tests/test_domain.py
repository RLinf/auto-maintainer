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

"""Pure domain rules: review states, scoring, merge gate, markers, commands, CODEOWNERS, calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from auto_maintainer.domain.calendar import BusinessCalendar
from auto_maintainer.domain.codeowners import (
    owners_for,
    owners_of_paths,
    parse_codeowners,
)
from auto_maintainer.domain.events import parse_commands
from auto_maintainer.domain.markers import build_marker, parse_markers
from auto_maintainer.domain.merge_gate import (
    MergeArm,
    evaluate_merge_gate,
    squash_message,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import RepoProfile, Timing
from auto_maintainer.domain.review import (
    ReviewResult,
    ReviewState,
    classify_review_state,
    render_review,
)
from auto_maintainer.domain.scoring import (
    DEFAULT_RUBRIC,
    Evaluation,
    Rubric,
    StageGoal,
    recompute,
)
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    IssueComment,
    Mergeable,
    RepoInfo,
    Review,
    ReviewEventState,
    ReviewThread,
)
from auto_maintainer.testing.fakes import make_pr

BOT = "bot[bot]"
T0 = datetime(2026, 1, 5, 9, tzinfo=UTC)


def _review(
    state: ReviewEventState,
    sha: str,
    minutes: int = 0,
    author: str = BOT,
    body: str = "x",
) -> Review:
    return Review(
        id=minutes + 1,
        author=author,
        state=state,
        commit_sha=sha,
        submitted_at=T0 + timedelta(minutes=minutes),
        body=body,
    )


class TestReviewState:
    def test_new_self_draft_closed(self) -> None:
        assert classify_review_state(make_pr(), {BOT}).state == ReviewState.NEW_REVIEW
        assert (
            classify_review_state(make_pr(author=BOT), {BOT}).state
            == ReviewState.SELF_PR
        )
        assert (
            classify_review_state(make_pr(is_draft=True), {BOT}).state
            == ReviewState.DRAFT
        )
        assert (
            classify_review_state(
                make_pr(is_draft=True), {BOT}, include_drafts=True
            ).state
            == ReviewState.NEW_REVIEW
        )

    def test_followup_and_up_to_date(self) -> None:
        pr = make_pr(
            sha="b" * 40, reviews=[_review(ReviewEventState.COMMENTED, "a" * 40)]
        )
        cls = classify_review_state(pr, {BOT})
        assert cls.state == ReviewState.FOLLOWUP_REVIEW and cls.reviewed_sha == "a" * 40
        assert (
            classify_review_state(
                pr.model_copy(update={"head_sha": "a" * 40}), {BOT}
            ).state
            == ReviewState.UP_TO_DATE
        )

    def test_approved_new_commits(self) -> None:
        pr = make_pr(
            sha="b" * 40, reviews=[_review(ReviewEventState.APPROVED, "a" * 40)]
        )
        assert (
            classify_review_state(pr, {BOT}).state == ReviewState.APPROVED_NEW_COMMITS
        )

    def test_marked_comments_count_and_unmarked_do_not(self) -> None:
        plain = IssueComment(id=1, author=BOT, body="thanks", created_at=T0)
        assert (
            classify_review_state(make_pr(comments=[plain]), {BOT}).state
            == ReviewState.NEW_REVIEW
        )
        marked = IssueComment(
            id=2,
            author=BOT,
            body=build_marker("review", 42, "a" * 40, "act_1") + "\nbody",
            created_at=T0,
        )
        assert (
            classify_review_state(make_pr(comments=[marked]), {BOT}).state
            == ReviewState.UP_TO_DATE
        )
        legacy = IssueComment(
            id=3, author="lead", body="<!-- old-bot: sha=abcdef1 -->", created_at=T0
        )
        pr = make_pr(comments=[legacy])
        assert (
            classify_review_state(
                pr, {"lead"}, [r"<!--\s*old-bot:\s*sha=([0-9a-f]{7,40})\s*-->"]
            ).state
            == ReviewState.FOLLOWUP_REVIEW
        )

    def test_dismissed_without_body_is_ignored(self) -> None:
        pr = make_pr(reviews=[_review(ReviewEventState.DISMISSED, "a" * 40, body="")])
        assert classify_review_state(pr, {BOT}).state == ReviewState.NEW_REVIEW


def test_render_review_puts_diff_findings_inline() -> None:
    profile = RepoProfile(repo="acme/widget")
    result = ReviewResult.model_validate(
        {
            "summary": "S",
            "findings": [
                {
                    "id": "F1",
                    "severity": "major",
                    "category": "correctness",
                    "title": "A",
                    "body": "a",
                    "path": "x.py",
                    "line": 3,
                    "suggestion": "y = 1",
                },
                {
                    "id": "F2",
                    "severity": "minor",
                    "category": "docs",
                    "title": "B",
                    "body": "b",
                    "path": "README.md",
                    "line": 1,
                },
            ],
            "recommendation": "Changes requested",
        }
    )
    out = render_review(result, profile, {"x.py"})
    assert [c.path for c in out.comments] == [
        "x.py"
    ] and "```suggestion" in out.comments[0].body
    assert (
        out.body.startswith("### Summary")
        and "(`x.py:3`)" in out.body
        and "  b" in out.body
    )


def test_scoring_boosts_are_capped_and_expire() -> None:
    goals = [
        StageGoal(id="A", title="a", keywords=["ascend"], boost=4),
        StageGoal(id="B", title="b", keywords=["npu"], boost=4),
        StageGoal(
            id="C",
            title="c",
            keywords=["npu"],
            boost=4,
            expires_at=T0 - timedelta(days=1),
        ),
    ]
    ev = Evaluation(
        repo="r/r",
        number=1,
        scores={"impact": 8, "urgency": 6, "strategic": 7, "readiness": 9},
    )
    out = recompute(ev, DEFAULT_RUBRIC, goals, "Ascend NPU support", now=T0)
    assert (
        out.base == 74.5 and out.boost == 5.0 and out.total == 79.5 and out.tier == "P0"
    )
    assert out.matched_goals == ["A", "B"]
    pinned = recompute(
        ev.model_copy(update={"tier_override": "P3"}), DEFAULT_RUBRIC, goals, "", now=T0
    )
    assert pinned.tier == "P3"
    with pytest.raises(ValueError, match="sum to 1.0"):
        Rubric(dimensions={"a": {"weight": 0.5}})


def _gate_pr(**kw):
    defaults = {
        "reviews": [
            Review(
                id=1,
                author="bob",
                state=ReviewEventState.APPROVED,
                commit_sha="a" * 40,
                submitted_at=T0,
            )
        ],
        "checks": [
            CheckRun(
                id=1,
                name="unit",
                workflow="CI",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.SUCCESS,
            )
        ],
        "mergeable": Mergeable.MERGEABLE,
    }
    defaults.update(kw)
    return make_pr(**defaults)


def _gate(pr, profile=None, arm=True, **kw):
    profile = profile or RepoProfile.model_validate(
        {
            "repo": "acme/widget",
            "merge": {"enabled": True},
            "ci": {"required_workflows": ["CI"]},
            "labels": {"roles": {"ci_gate": "run-ci", "changes_requested": "waiting"}},
        }
    )
    a = (
        MergeArm(
            repo="acme/widget",
            number=42,
            sha=pr.head_sha,
            armed_by="maria",
            armed_at=T0,
            armer_permission=Permission.MAINTAIN,
        )
        if arm
        else None
    )
    args = {
        "reviewer_permissions": {"bob": Permission.WRITE},
        "service_identities": {BOT},
        "blocking_checks": {},
        "paused": False,
        "now": T0,
    }
    args.update(kw)
    return evaluate_merge_gate(pr, profile, RepoInfo(name="acme/widget"), a, **args)


class TestMergeGate:
    def test_merges_when_everything_holds(self) -> None:
        assert _gate(_gate_pr()).outcome == "merge"

    def test_needs_an_arm_and_a_human_approval(self) -> None:
        assert _gate(_gate_pr(), arm=False).outcome == "blocked"
        assert (
            _gate(_gate_pr(), reviewer_permissions={"bob": Permission.READ}).outcome
            == "blocked"
        )
        bot = _gate_pr(
            reviews=[
                Review(
                    id=1, author=BOT, state=ReviewEventState.APPROVED, submitted_at=T0
                )
            ]
        )
        assert (
            _gate(bot, reviewer_permissions={BOT: Permission.ADMIN}).outcome
            == "blocked"
        )

    def test_waits_for_ci_and_asks_for_the_gate_label(self) -> None:
        running = _gate_pr(
            checks=[
                CheckRun(
                    id=1, name="unit", workflow="CI", status=CheckStatus.IN_PROGRESS
                )
            ]
        )
        assert _gate(running).outcome == "wait"
        missing = _gate(_gate_pr(checks=[]))
        assert missing.outcome == "wait" and missing.needs_ci_gate

    def test_blocks_on_threads_labels_conflicts_and_hygiene(self) -> None:
        assert (
            _gate(_gate_pr(threads=[ReviewThread(id="t", is_resolved=False)])).outcome
            == "blocked"
        )
        assert _gate(_gate_pr(labels=["waiting"])).outcome == "blocked"
        assert _gate(_gate_pr(mergeable=Mergeable.CONFLICTING)).outcome == "blocked"
        assert (
            _gate(_gate_pr(), blocking_checks={"signoff": False}).outcome == "blocked"
        )
        assert _gate(_gate_pr(), paused=True).outcome == "blocked"

    def test_arm_must_match_head_and_branch_updates(self) -> None:
        moved = _gate_pr().model_copy(update={"head_sha": "b" * 40})
        a = MergeArm(
            repo="acme/widget",
            number=42,
            sha="a" * 40,
            armed_by="maria",
            armed_at=T0,
            armer_permission=Permission.MAINTAIN,
        )
        profile = RepoProfile.model_validate(
            {"repo": "acme/widget", "merge": {"enabled": True}}
        )
        result = evaluate_merge_gate(
            moved,
            profile,
            RepoInfo(name="acme/widget"),
            a,
            {"bob": Permission.WRITE},
            {BOT},
            {},
            False,
            T0,
        )
        assert result.outcome == "blocked"
        behind = _gate(_gate_pr(behind_base=True))
        assert behind.outcome == "update_branch"

    def test_squash_message_keeps_trailers_once(self) -> None:
        profile = RepoProfile.model_validate(
            {
                "repo": "acme/widget",
                "merge": {
                    "message": {
                        "preserve_trailers": ["Signed-off-by", "Co-authored-by"]
                    }
                },
            }
        )
        pr = make_pr(
            commits=[
                Commit(sha="1" * 40, message="feat: a\n\nSigned-off-by: A <a@x>"),
                Commit(
                    sha="2" * 40,
                    message="fix: b\n\nSigned-off-by: A <a@x>\nCo-authored-by: B <b@x>\nReviewed-by: C",
                ),
            ]
        )
        subject, body = squash_message(pr, profile)
        assert subject.endswith("(#42)")
        assert (
            body.count("Signed-off-by: A <a@x>") == 1
            and "Co-authored-by: B <b@x>" in body
            and "Reviewed-by" not in body
        )


def test_markers_and_commands() -> None:
    m = parse_markers(build_marker("hygiene", 3, "f" * 40, "act_x") + "\ntext")
    assert m[0].kind == "hygiene" and m[0].pr == 3 and m[0].action == "act_x"
    cmds = parse_commands(
        "/am merge\n> /am merge cancel\n```\n/am pause\n```\n/am label bug docs\n/am nope"
    )
    assert [(c.name, c.args) for c in cmds] == [
        ("merge", []),
        ("label", ["bug", "docs"]),
    ]
    assert parse_commands("/am merge cancel")[0].name == "merge_cancel"


def test_codeowners_last_match_wins() -> None:
    rules = parse_codeowners("* @all\n/docs/ @writer\n*.py @py\n/src/core/** @core\n")
    assert owners_for(rules, "README.md") == ("all",)
    assert owners_for(rules, "docs/x.md") == ("writer",)
    assert owners_for(rules, "tools/x.py") == ("py",)
    assert owners_for(rules, "src/core/a/b.py") == ("core",)
    assert owners_of_paths(rules, ["docs/x.md", "a.py"]) == ["writer", "py"]


def test_business_days() -> None:
    cal = BusinessCalendar(
        Timing(holidays=[date(2026, 1, 7)], workdays=[date(2026, 1, 10)])
    )
    monday = datetime(2026, 1, 5, 10, tzinfo=UTC)
    friday = datetime(2026, 1, 9, 18, tzinfo=UTC)
    assert cal.business_days_between(monday, friday) == 3, (
        "Tue, Thu, Fri; Wed is a holiday"
    )
    assert cal.business_days_between(monday, friday + timedelta(days=1)) == 4, (
        "Sat the 10th is a working day"
    )
    assert cal.business_days_between(monday, friday + timedelta(days=2)) == 4, (
        "Sun is a weekend day"
    )


def test_commit_trailers() -> None:
    c = Commit(
        sha="1" * 40,
        message="feat: x\n\nbody\n\nSigned-off-by: A <a@x>\nCo-authored-by: B <b@x>",
    )
    assert c.trailers == [("Signed-off-by", "A <a@x>"), ("Co-authored-by", "B <b@x>")]
    assert Commit(sha="1" * 40, message="feat: x").trailers == []
