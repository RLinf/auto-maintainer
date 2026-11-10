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

from auto_maintainer.domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
)
from auto_maintainer.domain.calendar import BusinessCalendar
from auto_maintainer.domain.codeowners import (
    code_owner_report,
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
from auto_maintainer.domain.records import ActionRecord, PrAssignee
from auto_maintainer.domain.review import (
    QualityAssessment,
    ReviewResult,
    ReviewState,
    classify_review_state,
    quality_band,
    render_review,
    review_quality,
)
from auto_maintainer.domain.review_progress import review_progress
from auto_maintainer.domain.scoring import (
    DEFAULT_RUBRIC,
    Evaluation,
    Rubric,
    StageGoal,
    priority_key,
    recompute,
)
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    IssueComment,
    Mergeable,
    PrFile,
    RepoInfo,
    Review,
    ReviewEventState,
    ReviewThread,
    ThreadComment,
)
from auto_maintainer.testing.fakes import make_pr

BOT = "bot[bot]"
T0 = datetime(2026, 1, 5, 9, tzinfo=UTC)


def _review(
    state: ReviewEventState,
    sha: str,
    minutes: int = 0,
    author: str = BOT,
    body: str | None = None,
) -> Review:
    """A review; by default one the service posted, with its marker."""
    return Review(
        id=minutes + 1,
        author=author,
        state=state,
        commit_sha=sha,
        submitted_at=T0 + timedelta(minutes=minutes),
        body=build_marker("review", 42, sha, f"act_{minutes}") + "\nx"
        if body is None
        else body,
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

    def test_a_review_written_by_hand_does_not_count(self) -> None:
        """The service may act as a maintainer's own account; that maintainer's own
        approval is not a review by the service."""
        lgtm = _review(ReviewEventState.APPROVED, "a" * 40, body="LGTM!")
        pr = make_pr(sha="a" * 40, reviews=[lgtm])
        assert classify_review_state(pr, {BOT}).state == ReviewState.NEW_REVIEW
        ours = _review(ReviewEventState.COMMENTED, "a" * 40, minutes=1)
        pr = make_pr(sha="a" * 40, reviews=[ours, lgtm])
        cls = classify_review_state(pr, {BOT})
        assert cls.state == ReviewState.UP_TO_DATE
        assert cls.latest_review_state == "COMMENTED"

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


DIFF = """diff --git a/x.py b/x.py
index 1..2 100644
--- a/x.py
+++ b/x.py
@@ -10,6 +10,8 @@ def f():
 a
+b
+c
 d
@@ -40 +42,2 @@
-e
+f
+g
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,3 +0,0 @@
-x
-y
-z
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+p
+q
"""


def test_the_lines_a_diff_shows_are_where_inline_comments_go() -> None:
    from auto_maintainer.domain.diffs import commentable, fits, hunk_of

    ranges = commentable(DIFF)
    assert ranges["x.py"] == {
        "LEFT": [(10, 15), (40, 40)],
        "RIGHT": [(10, 17), (42, 43)],
    }
    assert ranges["gone.py"] == {"LEFT": [(1, 3)], "RIGHT": []}
    assert ranges["new.py"]["RIGHT"] == [(1, 2)]
    assert fits(ranges, "x.py", 12, 10) is True
    assert fits(ranges, "x.py", 42, 17) is False  # spans two hunks
    assert fits(ranges, "x.py", 30) is False
    assert fits(ranges, "x.py", 40, side="LEFT") is True
    assert fits(ranges, "elsewhere.py", 1) is None  # the diff does not reach it
    assert hunk_of(ranges, "x.py", 43) == (42, 43)


def test_render_review_puts_only_lines_the_diff_shows_inline() -> None:
    from auto_maintainer.domain.diffs import commentable

    def finding(n: int, line: int, start: int | None = None, **kw: str) -> dict:
        return {
            "id": f"F{n}",
            "severity": "minor",
            "category": "correctness",
            "title": f"T{n}",
            "body": f"body {n}",
            "path": "x.py",
            "line": line,
            "start_line": start,
            **kw,
        }

    result = ReviewResult.model_validate(
        {
            "summary": "S",
            "findings": [
                finding(1, 12),
                finding(2, 30),  # between the hunks
                finding(3, 43, 35),  # starts before its hunk: cut to it
                finding(4, 43, 35, suggestion="h"),  # a suggestion keeps its lines
            ],
            "recommendation": "Changes requested",
        }
    )
    out = render_review(
        result, RepoProfile(repo="acme/widget"), {"x.py"}, ranges=commentable(DIFF)
    )
    assert [(c.line, c.start_line) for c in out.comments] == [(12, None), (43, 42)]
    assert "(`x.py:30`)" in out.body and "  body 2" in out.body
    assert "(`x.py:43`)" in out.body and "  body 4" in out.body


def test_quality_bands_and_lenient_scores() -> None:
    assert [quality_band(n) for n in (100, 90, 89, 75, 60, 59, 40, 39, 0)] == [
        "excellent",
        "excellent",
        "good",
        "good",
        "fair",
        "poor",
        "poor",
        "bad",
        "bad",
    ]
    base = {"summary": "s", "recommendation": "Ready"}
    scored = ReviewResult.model_validate({**base, "quality": {"score": 87.6}})
    assert scored.quality is not None and scored.quality.score == 88
    clamped = ReviewResult.model_validate({**base, "quality": {"score": 130}})
    assert clamped.quality is not None and clamped.quality.score == 100
    assert ReviewResult.model_validate({**base, "quality": "great"}).quality is None


def _review_record(
    kind: str, quality: int | None, minute: int, status=ActionStatus.EXECUTED
) -> ActionRecord:
    result = {"quality": {"score": quality, "reason": f"r{minute}"}} if quality else {}
    return ActionRecord(
        action=Action(
            kind=ActionKind.REVIEW_POST,
            repo="acme/widget",
            number=42,
            head_sha=f"{minute:040x}",
            payload={"body": "b", "kind": kind, "result": result},
            created_at=T0 + timedelta(minutes=minute),
        ),
        status=status,
    )


def test_quality_is_the_latest_review_that_judged_one() -> None:
    first = _review_record("review", 62, 1)
    followup = _review_record("followup", 81, 2)
    q = review_quality([followup, first])
    assert q is not None and (q.score, q.band, q.kind, q.reason) == (
        81,
        "good",
        "followup",
        "r2",
    )

    rejected = _review_record("followup", 30, 3, ActionStatus.DENIED)
    assert review_quality([first, followup, rejected]) == q, (
        "a rejected draft is ignored"
    )

    draft = _review_record("followup", 70, 4, ActionStatus.PROPOSED)
    q = review_quality([first, followup, draft])
    assert q is not None and (q.score, q.posted) == (70, False)

    unjudged = _review_record("followup", None, 5)
    q = review_quality([first, followup, unjudged])
    assert q is not None and q.score == 81, "falls back to the last review that judged"

    assessed = QualityAssessment(
        score=55, reason="again", action_id=unjudged.action.id, at=T0
    )
    q = review_quality([first, followup, unjudged], assessed)
    assert q is not None and (q.score, q.rescored, q.band) == (55, True, "poor")
    assert review_quality([_review_record("review", None, 1)]) is None


def test_a_skills_own_headings_are_not_doubled() -> None:
    result = ReviewResult.model_validate(
        {
            "summary": "s",
            "recommendation": "Ready",
            "sections": {"### 1. What settings did you use?": "A800, 8 GPUs."},
        }
    )
    out = render_review(result, RepoProfile(repo="acme/widget"), set(), own_format=True)
    assert out.body.startswith("### 1. What settings did you use?\n\nA800")


def test_a_skills_own_format_lists_the_findings_its_sections_refer_to() -> None:
    finding = {"severity": "minor", "category": "docs", "title": "t"}
    result = ReviewResult.model_validate(
        {
            "summary": "s",
            "recommendation": "Changes requested",
            "sections": {"### 9. What is your recommendation?": "Fix `F1` and `F2`."},
            "findings": [
                {**finding, "id": "F1", "severity": "major", "body": "Push it."},
                {
                    **finding,
                    "id": "F2",
                    "body": "Rename it.",
                    "path": "a.py",
                    "line": 3,
                },
            ],
        }
    )
    profile = RepoProfile(repo="acme/widget")
    out = render_review(result, profile, {"a.py"}, own_format=True)
    assert out.body == (
        "### 9. What is your recommendation?\n\nFix `F1` and `F2`.\n\n"
        "### Findings\n\n"
        "- **F1 · major · docs: t**\n  Push it.\n"
        "- **F2 · minor · docs: t** (`a.py:3`)"
    )
    assert out.comments[0].body == "Rename it.", "inline text keeps the skill's form"

    own_list = result.model_copy(
        update={"sections": {**result.sections, "Findings": "- F1, F2"}}
    )
    out = render_review(own_list, profile, {"a.py"}, own_format=True)
    assert out.body.count("Findings") == 1, "a skill's own findings section stays"
    clean = result.model_copy(update={"findings": []})
    out = render_review(clean, profile, set(), own_format=True)
    assert "Findings" not in out.body


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


def test_code_owner_report_groups_files_and_reads_verdicts() -> None:
    """Files go to the owners of their last matching rule; a part counts as
    approved by a user owner's standing approval, never the author's; a team is
    only asked or not; a later comment leaves an approval standing."""
    rules = parse_codeowners(
        "* @lead\n/docs/ @writer @acme/docs\n/src/core/ @core @alice\n/vendor/\n"
    )
    sha = "b" * 40
    pr = make_pr(
        sha=sha,
        author="alice",
        files=[
            PrFile(path="src/core/a.py"),
            PrFile(path="src/core/b.py"),
            PrFile(path="docs/x.md"),
            PrFile(path="vendor/lib.c"),
            PrFile(path="README.md"),
        ],
        requested_reviewers=["lead", "walt"],
        requested_teams=["docs"],
        reviews=[
            Review(
                id=1,
                author="core",
                state=ReviewEventState.APPROVED,
                commit_sha="a" * 40,
            ),
            Review(
                id=2, author="core", state=ReviewEventState.COMMENTED, commit_sha=sha
            ),
            Review(
                id=3, author="alice", state=ReviewEventState.APPROVED, commit_sha=sha
            ),
            Review(
                id=4,
                author="writer",
                state=ReviewEventState.CHANGES_REQUESTED,
                commit_sha=sha,
            ),
        ],
    )
    report = code_owner_report(rules, pr)
    parts = {tuple(p.owners): p for p in report.parts}
    assert parts[("core", "alice")].files == ["src/core/a.py", "src/core/b.py"]
    assert parts[("core", "alice")].approved_by == ["core"], "not the author"
    assert parts[("writer", "acme/docs")].approved_by == []
    assert parts[("lead",)].files == ["README.md"]
    assert report.unowned == ["vendor/lib.c"], "a rule without owners owns nothing"
    people = {p.login: p for p in report.people}
    assert (people["core"].verdict, people["core"].at_head) == ("APPROVED", False)
    assert people["alice"].author and people["core"].files == 2
    assert people["writer"].verdict == "CHANGES_REQUESTED"
    assert people["acme/docs"].team and people["acme/docs"].requested
    assert people["lead"].requested and not people["core"].requested
    assert "walt" not in people


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


def test_review_progress_counts_each_reviewers_resolved_threads_over_time() -> None:
    """A reviewer's feedback is the threads they opened; resolved ones are
    addressed, outdated ones count apart. The author's own threads are notes. The
    timeline puts the pushes between the reviews, and each review carries the
    threads it opened."""
    head = "c" * 40

    def thread(i: int, who: str, minutes: int, resolved=False, outdated=False):
        return ReviewThread(
            id=f"t{i}",
            is_resolved=resolved,
            is_outdated=outdated,
            comments=[
                ThreadComment(
                    id=i, author=who, created_at=T0 + timedelta(minutes=minutes)
                )
            ],
        )

    pr = make_pr(
        sha=head,
        author="alice",
        created_at=T0,
        commits=[
            Commit(sha="a" * 40, committed_at=T0 - timedelta(hours=1)),
            Commit(sha="b" * 40, committed_at=T0 + timedelta(minutes=90)),
            Commit(sha=head, committed_at=T0 + timedelta(minutes=95), message="fix: x"),
        ],
        reviews=[
            Review(
                id=1,
                author="bob",
                state=ReviewEventState.CHANGES_REQUESTED,
                submitted_at=T0 + timedelta(minutes=60),
                commit_sha="a" * 40,
            ),
            Review(
                id=2,
                author="carol",
                state=ReviewEventState.APPROVED,
                submitted_at=T0 + timedelta(minutes=120),
                commit_sha=head,
            ),
            Review(
                id=3,
                author="bob",
                state=ReviewEventState.COMMENTED,
                submitted_at=T0 + timedelta(minutes=130),
                commit_sha=head,
            ),
        ],
        threads=[
            thread(1, "bob", 59, resolved=True),
            thread(2, "bob", 59, outdated=True),
            thread(3, "bob", 59),
            thread(4, "bob", 129, resolved=True),
            thread(5, "alice", 70),
        ],
        requested_reviewers=["dave"],
    )
    roles = [
        PrAssignee(
            repo=pr.repo,
            number=pr.number,
            login="erin",
            role="reviewer",
            kind="assigned",
            by="maria",
            at=T0 + timedelta(minutes=5),
        )
    ]
    progress = review_progress(pr, roles)
    people = {r.login: r for r in progress.reviewers}
    assert [r.login for r in progress.reviewers] == ["erin", "bob", "carol"], (
        "assigned reviewers first; dave, whom GitHub only asks, has not reviewed"
    )
    bob = people["bob"]
    assert (bob.verdict, bob.at_head, bob.reviews) == ("CHANGES_REQUESTED", False, 2)
    assert (bob.threads, bob.resolved, bob.outdated, bob.addressed) == (4, 2, 1, 0.5)
    assert people["carol"].at_head and people["carol"].addressed is None
    assert people["erin"].assigned
    assert (progress.threads, progress.resolved, progress.addressed) == (4, 2, 0.5)
    assert progress.approved_at_head == ["carol"]
    assert progress.changes_requested_by == ["bob"]
    steps = [
        (e.kind, e.login, e.threads, e.resolved, e.commits) for e in progress.timeline
    ]
    assert steps == [
        ("opened", "alice", 0, 0, 1),
        ("role", "erin", 0, 0, 0),
        ("review", "bob", 3, 1, 0),
        ("commits", "alice", 0, 0, 2),
        ("review", "carol", 0, 0, 0),
        ("review", "bob", 1, 1, 0),
    ]


def test_priority_ranks_by_tier_before_score() -> None:
    """An override sets a PR's tier and leaves its score, so ranking goes by tier
    first: a P0 with a low score comes before a P1 with a high one."""

    def ev(n: int, total: float, tier: str | None) -> Evaluation:
        return Evaluation(repo="a/b", number=n, total=total, tier=tier)

    pinned, scored, low = ev(1, 60.0, "P0"), ev(2, 75.0, "P1"), ev(3, 70.0, "P1")
    ranked = sorted(
        [scored, None, low, pinned], key=lambda e: priority_key(e, DEFAULT_RUBRIC)
    )
    assert ranked == [pinned, scored, low, None]
