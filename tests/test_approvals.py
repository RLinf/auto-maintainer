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

"""A PR's approvals against its branch's rules, as GitHub counts them: a number of
approvals from people with write access, apart from the code owners' approval of
each changed file."""

from __future__ import annotations

from auto_maintainer.domain.approvals import approval_status
from auto_maintainer.domain.codeowners import code_owner_report, parse_codeowners
from auto_maintainer.domain.snapshot import PrFile, RepoInfo, Review, ReviewEventState
from auto_maintainer.testing.fakes import make_pr

HEAD = "b" * 40
RULES = parse_codeowners("* @lin\n/rlinf/robotics/ @guo @lin\n")


def _pr(*reviews: Review, decision: str | None = "REVIEW_REQUIRED"):
    pr = make_pr(
        sha=HEAD,
        author="quanlu",
        files=[PrFile(path="docs/a.md"), PrFile(path="rlinf/robotics/b.py")],
        reviews=list(reviews),
    )
    return pr.model_copy(update={"review_decision": decision})


def _info(**kw: object) -> RepoInfo:
    return RepoInfo(
        name="RLinf/RLinf",
        default_branch="main",
        required_approvals=2,
        requires_code_owner_reviews=True,
        **kw,
    )


def _approve(i: int, who: str, sha: str = HEAD) -> Review:
    return Review(id=i, author=who, state=ReviewEventState.APPROVED, commit_sha=sha)


def test_one_code_owner_covers_the_files_but_the_branch_wants_two_approvals() -> None:
    """#1761: Lin owns every file and approved, so no code owner is missing; the branch
    still requires two approvals, so one more is needed from anyone who may write."""
    pr = _pr(_approve(1, "lin"))
    status = approval_status(pr, _info(), code_owner_report(RULES, pr), {"lin"})
    assert (status.required, status.approved_by, status.missing) == (2, ["lin"], 1)
    assert status.code_owners_waiting == [] and status.decision == "REVIEW_REQUIRED"


def test_whose_approvals_count() -> None:
    pr = _pr(
        _approve(1, "lin"),
        _approve(2, "quanlu"),
        _approve(3, "visitor"),
        _approve(4, "guo", sha="a" * 40),
        Review(id=5, author="zhu", state=ReviewEventState.CHANGES_REQUESTED),
    )
    writers = {"lin", "guo", "zhu", "quanlu"}
    status = approval_status(pr, _info(), code_owner_report(RULES, pr), writers)
    assert status.approved_by == ["lin", "guo"], "not the author's own"
    assert [(n.login, n.reason) for n in status.not_counted] == [
        ("visitor", "no write access")
    ]
    assert status.missing == 0 and status.changes_requested_by == ["zhu"]

    last_push = _info(requires_last_push_approval=True)
    status = approval_status(pr, last_push, code_owner_report(RULES, pr), writers)
    assert status.approved_by == ["lin"] and status.missing == 1
    assert ("guo", "approved an earlier commit") in [
        (n.login, n.reason) for n in status.not_counted
    ]


def test_a_later_review_replaces_an_earlier_one() -> None:
    pr = _pr(
        _approve(1, "lin"),
        Review(id=2, author="lin", state=ReviewEventState.CHANGES_REQUESTED),
    )
    status = approval_status(pr, _info(), code_owner_report(RULES, pr), {"lin"})
    assert status.approved_by == [] and status.changes_requested_by == ["lin"]
    assert status.missing == 2 and status.code_owners_waiting == [
        ["lin"],
        ["guo", "lin"],
    ]


def test_rules_of_another_base_branch_are_not_guessed() -> None:
    pr = _pr(_approve(1, "lin"), decision=None)
    status = approval_status(pr, None, code_owner_report(RULES, pr), {"lin"})
    assert (status.required, status.missing, status.decision) == (None, None, None)
    assert status.approved_by == ["lin"]
