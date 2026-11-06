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

"""End-to-end flows on fakes (design section 6)."""

from __future__ import annotations

from pathlib import Path

from auto_maintainer.domain.actions import ActionStatus, ProposalState
from auto_maintainer.domain.events import EventKind
from auto_maintainer.domain.markers import parse_markers
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    PrFile,
    Review,
    ReviewEventState,
)
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness

LABELS = {
    "needs_review": "needs review",
    "changes_requested": "waiting on author",
    "ci_gate": "run-ci",
    "community": "community",
}

REVIEW = {
    "summary": "Guards against empty batches in the loader.",
    "findings": [
        {
            "id": "F1",
            "severity": "major",
            "category": "correctness",
            "title": "Off-by-one in the batch loop",
            "body": "The loop skips the last batch when the size divides evenly.",
            "path": "src/loader.py",
            "line": 12,
            "suggestion": "for i in range(n + 1):",
        }
    ],
    "recommendation": "Changes requested",
}


def _signed(msg: str) -> str:
    return f"{msg}\n\nSigned-off-by: Alice <alice@example.com>"


def _pr(**kw):
    defaults = {
        "files": [PrFile(path="src/loader.py", additions=10, deletions=2)],
        "commits": [
            Commit(sha="c" * 40, message=_signed("fix(core): handle empty batches"))
        ],
    }
    defaults.update(kw)
    return make_pr(**defaults)


async def _auto(h: Harness, **levels: str) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    path = profile_path("acme/widget")
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("policy", {}).update(
        {k.replace("_", ".", 1): {"level": v} for k, v in levels.items()}
    )
    layer.setdefault("labels", {})["roles"] = LABELS
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly("acme/widget", "maria")


async def test_new_pr_in_shadow_writes_nothing(harness: Harness) -> None:
    harness.forge.put_pr(_pr())
    harness.runner.queue(
        "triage", {"summary": "Fixes empty batches.", "labels": ["bug"]}
    )
    harness.runner.queue(
        "evaluate",
        {
            "scores": {"impact": 6, "urgency": 7, "strategic": 5, "readiness": 8},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.queue("review", REVIEW)
    harness.runner.queue("verify", {"verdicts": [{"id": "F1", "confirmed": True}]})
    events = await harness.poll()
    assert [e.kind for e in events] == [EventKind.PR_OPENED]
    assert harness.forge.writes == [], "defaults keep every repository action in shadow"
    shadowed = await harness.store.list_actions(statuses=[ActionStatus.SHADOWED])
    kinds = {r.action.kind for r in shadowed}
    assert {"label.apply", "review.post"} <= {str(k) for k in kinds}
    evaluation = await harness.store.get_evaluation("acme/widget", 42)
    assert (
        evaluation is not None
        and evaluation.tier in {"P0", "P1", "P2", "P3"}
        and evaluation.total > 0
    )


async def test_review_is_proposed_then_posted_with_marker_and_footer(
    harness: Harness,
) -> None:
    await _auto(harness, label_apply="auto", review_post="propose")
    harness.forge.put_pr(_pr())
    harness.runner.queue(
        "triage", {"summary": "Fixes empty batches.", "labels": ["bug"]}
    )
    harness.runner.queue("review", REVIEW)
    harness.runner.queue("verify", {"verdicts": [{"id": "F1", "confirmed": True}]})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    await harness.poll()

    labels = harness.forge.writes_of("add_labels")
    assert labels and set(labels[0]["labels"]) >= {"bug", "needs review", "community"}
    assert not harness.forge.writes_of("create_review")
    proposals = await harness.store.list_proposals(ProposalState.PENDING)
    review = [
        p
        for p in proposals
        if (await harness.store.get_action(p.action_id)).action.kind == "review.post"
    ]
    assert len(review) == 1

    await harness.service.pipeline.approve(review[0].id, "maria")
    await harness.settle()
    posted = harness.forge.writes_of("create_review")
    assert len(posted) == 1
    body = posted[0]["body"]
    markers = parse_markers(body)
    assert markers and markers[0].kind == "review" and markers[0].sha == "a" * 40
    assert "approved by maria" in body and "@maria" not in body
    assert posted[0]["comments"][0]["path"] == "src/loader.py"
    assert "```suggestion" in posted[0]["comments"][0]["body"]
    # the changes_requested role follows a "Changes requested" recommendation
    assert any(
        "waiting on author" in w["labels"]
        for w in harness.forge.writes_of("add_labels")
    )


async def test_followup_after_new_commits(harness: Harness) -> None:
    await _auto(harness, review_post="auto", thread_reply="auto", label_apply="auto")
    harness.forge.put_pr(_pr())
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.queue("review", REVIEW)
    harness.runner.queue("verify", {"verdicts": [{"id": "F1", "confirmed": True}]})
    await harness.poll()
    assert len(harness.forge.writes_of("create_review")) == 1

    pr = await harness.forge.get_pr("acme/widget", 42)
    harness.forge.put_pr(pr.model_copy(update={"head_sha": "b" * 40}))
    harness.runner.queue(
        "followup",
        {
            "statuses": [
                {
                    "id": "F1",
                    "title": "Off-by-one",
                    "status": "resolved",
                    "fixed_in": "b" * 40,
                }
            ],
            "new_findings": [],
            "recommendation": "Ready",
        },
    )
    events = await harness.poll()
    assert EventKind.PR_SYNCHRONIZED in [e.kind for e in events]
    reviews = harness.forge.writes_of("create_review")
    assert len(reviews) == 2
    assert "All earlier review comments are resolved" in reviews[1]["body"]
    replies = harness.forge.writes_of("reply_to_review_comment")
    assert replies and "Resolved in `bbbbbbb`" in replies[0]["body"]


async def test_flaky_ci_is_rerun_once_and_budget_holds(harness: Harness) -> None:
    await _auto(harness, ci_rerun_failed="auto")
    check = CheckRun(
        id=7,
        name="unit",
        workflow="CI",
        status=CheckStatus.COMPLETED,
        conclusion=CheckConclusion.FAILURE,
        run_id=99,
        run_attempt=1,
    )
    harness.forge.put_pr(
        _pr(
            checks=[
                check.model_copy(
                    update={"status": CheckStatus.IN_PROGRESS, "conclusion": None}
                )
            ]
        )
    )
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.on("review", lambda t: REVIEW)
    harness.runner.on("verify", lambda t: {"verdicts": []})
    await harness.poll()
    harness.forge.job_logs[7] = "step 3\nnpm ERR! network ECONNRESET while fetching\n"
    pr = await harness.forge.get_pr("acme/widget", 42)
    harness.forge.put_pr(pr.model_copy(update={"checks": [check]}))
    events = await harness.poll()
    assert EventKind.CHECKS_COMPLETED in [e.kind for e in events]
    assert harness.forge.writes_of("rerun_failed_jobs") == [
        {"repo": "acme/widget", "run_id": 99}
    ]
    observations = await harness.store.list_ci_observations("acme/widget")
    assert (
        observations[-1].signature == "gha-network-reset"
        and observations[-1].category == "flaky"
    )


async def _failing_ci(harness: Harness, log: str) -> None:
    """Bring PR 42 through its first poll, then fail its ``unit`` job with ``log``."""
    check = CheckRun(
        id=8,
        name="unit",
        workflow="CI",
        status=CheckStatus.COMPLETED,
        conclusion=CheckConclusion.FAILURE,
        run_id=100,
        run_attempt=1,
    )
    harness.forge.put_pr(
        _pr(
            checks=[
                check.model_copy(
                    update={"status": CheckStatus.IN_PROGRESS, "conclusion": None}
                )
            ]
        )
    )
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.on("review", lambda t: REVIEW)
    harness.runner.on("verify", lambda t: {"verdicts": []})
    await harness.poll()
    harness.forge.job_logs[8] = log
    pr = await harness.forge.get_pr("acme/widget", 42)
    harness.forge.put_pr(pr.model_copy(update={"checks": [check]}))
    await harness.poll()


async def test_a_real_ci_failure_gets_its_root_cause_from_the_code(
    harness: Harness,
) -> None:
    await _auto(harness, comment_upsert="auto")
    harness.runner.on(
        "ci_diagnose",
        lambda t: {
            "category": "pr_caused",
            "confidence": 0.6,
            "explanation": "test_empty_batch fails",
        },
    )
    asked: list[str] = []

    def root_cause(task):
        asked.append(task.prompt)
        assert task.mode == "read_only"
        return {
            "causes": [
                {
                    "jobs": ["unit", "lint"],
                    "failing": "tests/test_loader.py::test_empty_batch",
                    "origin": "change",
                    "cause": "The new guard returns None for an empty batch, and the "
                    "caller iterates over the result.",
                    "culprits": [
                        {"path": "src/loader.py", "line": 12, "why": "returns None"},
                        {"path": "../../etc/passwd", "line": 1, "why": "outside"},
                    ],
                    "fix": "Return an empty list instead of None.",
                    "evidence": ["TypeError: 'NoneType' object is not iterable"],
                    "confidence": 0.8,
                }
            ]
        }

    harness.runner.on("ci_root_cause", root_cause)
    await _failing_ci(
        harness,
        "FAILED tests/test_loader.py::test_empty_batch\n"
        "TypeError: 'NoneType' object is not iterable\n",
    )
    assert asked and "test_empty_batch" in asked[0]
    [observation] = [
        o
        for o in await harness.store.list_ci_observations("acme/widget")
        if o.check_id == 8
    ]
    cause = observation.root_cause
    assert cause is not None and cause.jobs == ["unit"]  # lint did not fail here
    assert [c.path for c in cause.culprits] == ["src/loader.py"]
    [comment] = [
        w
        for w in harness.forge.writes_of("create_comment")
        if "CI failures" in w["body"]
    ]
    body = comment["body"]
    assert "tests/test_loader.py::test_empty_batch" in body
    assert "`src/loader.py:12`: returns None" in body
    assert "Suggested fix: Return an empty list instead of None." in body
    assert "0.8" not in body and "passwd" not in body


async def test_a_failure_the_change_did_not_cause_goes_to_the_maintainers(
    harness: Harness,
) -> None:
    await _auto(harness, comment_upsert="auto", notify="auto")
    harness.runner.on(
        "ci_diagnose",
        lambda t: {"category": "unknown", "confidence": 0.2, "explanation": "?"},
    )
    harness.runner.on(
        "ci_root_cause",
        lambda t: {
            "causes": [
                {
                    "jobs": ["unit"],
                    "failing": "tests/test_clock.py::test_tz",
                    "origin": "base",
                    "cause": "The test assumes the runner's time zone is UTC.",
                    "confidence": 0.7,
                }
            ]
        },
    )
    await _failing_ci(harness, "FAILED tests/test_clock.py::test_tz\n")
    [comment] = [
        w
        for w in harness.forge.writes_of("create_comment")
        if "CI failures" in w["body"]
    ]
    assert "does not come from this PR" in comment["body"]
    notes = [
        a
        for a in await harness.store.list_actions(repo="acme/widget")
        if a.action.kind == "notify"
    ]
    assert any(n.action.payload["event"] == "ci.base" for n in notes)


async def test_without_a_root_cause_the_first_look_still_explains(
    harness: Harness,
) -> None:
    await _auto(harness, comment_upsert="auto")
    harness.runner.on(
        "ci_diagnose",
        lambda t: {
            "category": "pr_caused",
            "confidence": 0.6,
            "explanation": "test_empty_batch fails on the new guard",
        },
    )
    harness.runner.on("ci_root_cause", lambda t: {"causes": []})
    await _failing_ci(harness, "FAILED tests/test_loader.py::test_empty_batch\n")
    [comment] = [
        w
        for w in harness.forge.writes_of("create_comment")
        if "CI failures" in w["body"]
    ]
    assert "**unit**: test_empty_batch fails on the new guard" in comment["body"]


async def test_merge_waits_for_gate_then_merges_with_trailers(harness: Harness) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    path = profile_path("acme/widget")
    layer = parse_yaml(harness.memory.files[path], path)
    layer["merge"] = {"enabled": True}
    layer["ci"] = {"required_workflows": ["CI"]}
    layer["policy"] = {
        "pr.merge": {"level": "auto"},
        "comment.upsert": {"level": "auto"},
    }
    harness.memory.files[path] = dump_yaml(layer)
    await harness.approve_directly("acme/widget", "maria")
    harness.forge.set_permission("acme/widget", "bob", Permission.WRITE)
    running = CheckRun(id=8, name="unit", workflow="CI", status=CheckStatus.IN_PROGRESS)
    pr = _pr(
        title="fix(core): handle empty batches",
        checks=[running],
        commits=[
            Commit(sha="c" * 40, message=_signed("fix(core): handle empty batches")),
            Commit(
                sha="e" * 40,
                message="test: cover it\n\nSigned-off-by: Alice <alice@example.com>\nCo-authored-by: Bo <bo@example.com>",
            ),
        ],
        reviews=[
            Review(
                id=1, author="bob", state=ReviewEventState.APPROVED, commit_sha="a" * 40
            )
        ],
    )
    harness.forge.put_pr(pr)
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.on(
        "review", lambda t: {**REVIEW, "findings": [], "recommendation": "Ready"}
    )
    await harness.poll()

    from auto_maintainer.domain.events import Event

    harness.forge.put_pr(
        (await harness.forge.get_pr("acme/widget", 42)).model_copy(
            update={"comments": []}
        )
    )
    from auto_maintainer.domain.snapshot import IssueComment

    pr_now = await harness.forge.get_pr("acme/widget", 42)
    harness.forge.put_pr(
        pr_now.model_copy(
            update={"comments": [IssueComment(id=5, author="maria", body="/am merge")]}
        )
    )
    await harness.poll()
    assert await harness.store.get_arm("acme/widget", 42) is not None
    assert not harness.forge.writes_of("merge"), "CI is still running"

    done = running.model_copy(
        update={"status": CheckStatus.COMPLETED, "conclusion": CheckConclusion.SUCCESS}
    )
    pr_now = await harness.forge.get_pr("acme/widget", 42)
    harness.forge.put_pr(pr_now.model_copy(update={"checks": [done]}))
    await harness.poll()
    merges = harness.forge.writes_of("merge")
    assert len(merges) == 1
    assert merges[0]["subject"] == "fix(core): handle empty batches (#42)"
    assert "Signed-off-by: Alice <alice@example.com>" in merges[0]["body"]
    assert "Co-authored-by: Bo <bo@example.com>" in merges[0]["body"]
    assert await harness.store.get_arm("acme/widget", 42) is None
    _ = Event


async def test_dev_mode_never_writes(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        layer={
            "policy": {"label.apply": {"level": "auto"}},
            "labels": {"roles": LABELS},
        },
        dev_mode=True,
    )
    try:
        h.forge.put_pr(_pr())
        h.runner.on("triage", lambda t: {"summary": "s", "labels": ["bug"]})
        h.runner.on(
            "evaluate",
            lambda t: {
                "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
                "summary": "s",
                "rationale": "r",
            },
        )
        h.runner.on("review", lambda t: REVIEW)
        h.runner.on("verify", lambda t: {"verdicts": []})
        await h.poll()
        assert h.forge.writes == []
    finally:
        await h.close()


async def test_agent_tasks_are_untrusted_unless_the_author_is_on_the_team(
    harness: Harness,
) -> None:
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.on(
        "review", lambda t: {**REVIEW, "findings": [], "recommendation": "Ready"}
    )
    harness.forge.put_pr(_pr(number=50, author="outsider"))
    harness.forge.put_pr(_pr(number=51, sha="b" * 40, author="maria"))
    await harness.poll()
    by_pr = {
        t.prompt.split("PR: #")[1].split(" ")[0]: t.untrusted_input
        for t in harness.runner.tasks
        if t.kind == "review"
    }
    assert by_pr == {"50": True, "51": False}
