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


"""Merge scheduling: maintainers schedule ready PRs, which take turns when they must
be up to date with the base; the schedule starts CI as the repository starts it,
reruns flakes, sends real failures to the failure lane, and merges what passes."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.actions import ActionKind, ActionStatus
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.merge_gate import MergeArm
from auto_maintainer.domain.merge_schedule import (
    ScheduleState,
    candidate_items,
    only_base_merged,
    state_key,
    turns,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import CiStart
from auto_maintainer.domain.run_ci import ci_view, latest_checks, plan_run_ci
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    PrFile,
    PrState,
    RepoInfo,
    Review,
    ReviewEventState,
)
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
RLINF_START = CiStart(gate_jobs=["check-changes"], summary_jobs=["ci-test-finish"])


def _job(
    id: int,
    name: str,
    conclusion: str | None,
    run_id: int,
    workflow: str = "CI Test",
    attempt: int = 1,
) -> CheckRun:
    return CheckRun(
        id=id,
        name=name,
        workflow=workflow,
        status=CheckStatus.COMPLETED if conclusion else CheckStatus.IN_PROGRESS,
        conclusion=CheckConclusion(conclusion) if conclusion else None,
        run_id=run_id,
        run_attempt=attempt,
    )


# -- reading CI ------------------------------------------------------------------------


def test_only_the_newest_run_of_a_workflow_counts() -> None:
    # A label started a second run; the concurrency group cancelled the first.
    old = _job(1, "check-changes", "failure", 100)
    cancelled = _job(2, "check-changes", "cancelled", 101)
    newest = _job(3, "unit", "success", 102, attempt=2)
    first_try = _job(4, "unit", "failure", 102)
    assert latest_checks([old, cancelled, first_try, newest]) == [newest]
    retried = _job(5, "lint", "success", 103, workflow="Lint", attempt=2)
    failed_before = _job(6, "lint", "failure", 103, workflow="Lint", attempt=1)
    assert latest_checks([failed_before, retried]) == [retried]


def test_gate_and_summary_jobs_are_never_the_failure() -> None:
    # RLinf without run-ci: the gate job fails on purpose, the summary job reports it.
    gated = make_pr(
        labels=[],
        checks=[
            _job(1, "check-changes", "failure", 100),
            _job(2, "unit-tests", "skipped", 100),
            _job(3, "ci-test-finish", "failure", 100),
        ],
    )
    view = ci_view(gated, "run-ci", RLINF_START)
    assert view.state == "gated" and view.failed == []
    # With the label but no run since, CI stopped at its gate: no rerun starts it.
    stopped = gated.model_copy(update={"labels": ["run-ci"]})
    assert ci_view(stopped, "run-ci", RLINF_START).state == "stopped"
    assert plan_run_ci(stopped, "run-ci", RLINF_START).state == "none"
    # A real test failure is the one named, never the summary job.
    real = stopped.model_copy(
        update={
            "checks": [
                _job(4, "check-changes", "success", 101),
                _job(
                    5,
                    "agent-reason-e2e-tests / wideseek-r1-qwen-grpo-test",
                    "failure",
                    101,
                ),
                _job(6, "ci-test-finish", "failure", 101),
            ]
        }
    )
    view = ci_view(real, "run-ci", RLINF_START)
    assert view.state == "failed"
    assert [c.name for c in view.failed] == [
        "agent-reason-e2e-tests / wideseek-r1-qwen-grpo-test"
    ]


def test_a_running_pr_keeps_its_turn_and_waiting_ones_follow_the_order() -> None:
    order = [41, 42, 43]
    states = {
        41: ScheduleState(phase="queued"),
        42: ScheduleState(phase="running"),
        43: ScheduleState(phase="queued"),
    }
    # 41 was moved ahead of 42 while 42 ran: 42 keeps its turn, one at a time.
    assert turns(order, states, True, 3) == [42]
    # Several at once: the running one, then the first waiting ones.
    assert turns(order, states, False, 2) == [42, 41]
    states[42] = ScheduleState(phase="failed")
    assert turns(order, states, True, 3) == [41]


def test_a_pr_is_schedulable_once_it_meets_the_repositorys_target() -> None:
    from auto_maintainer.domain.merge_gate import evaluate_merge_gate
    from auto_maintainer.domain.merge_schedule import left_for_merge, ready_conditions
    from auto_maintainer.domain.profile import RepoProfile

    info = RepoInfo(name=REPO, required_approvals=1, requires_code_owner_reviews=True)

    def gate(pr, profile):  # type: ignore[no-untyped-def]
        return evaluate_merge_gate(
            pr, profile, info, None, {"bob": Permission.WRITE}, set(), {}, False, T0
        )

    # By default, everything the merge rules hold a PR to but CI and being up to
    # date: approved by a writer, but GitHub still waits for a code owner.
    default = RepoProfile(repo=REPO)
    pr = make_pr(
        reviews=[Review(id=1, author="bob", state=ReviewEventState.APPROVED)],
        review_decision="REVIEW_REQUIRED",
        behind_base=True,
    )
    ready = ready_conditions(default)
    back = [i for i in candidate_items(gate(pr, default), ready) if not i.ok]
    assert [i.name for i in back] == ["approvals"] and "a code owner's" in back[
        0
    ].detail
    approved = pr.model_copy(update={"review_decision": "APPROVED"})
    assert not [
        i for i in candidate_items(gate(approved, default), ready) if not i.ok
    ], "behind main is fine: the schedule merges main in before CI"

    # A repository whose reviewers approve after CI schedules before the approvals,
    # and its PRs wait for them after their CI.
    after_ci = RepoProfile.model_validate(
        {
            "repo": REPO,
            "merge": {"schedule": {"ready": ["not_draft", "no_changes_requested"]}},
        }
    )
    ready = ready_conditions(after_ci)
    assert not [i for i in candidate_items(gate(pr, after_ci), ready) if not i.ok]
    assert [i.name for i in left_for_merge(gate(pr, after_ci), ready)] == ["approvals"]


def test_a_check_that_only_warns_may_stay_unpassed() -> None:
    from auto_maintainer.domain.merge_gate import evaluate_merge_gate
    from auto_maintainer.domain.profile import RepoProfile

    # GitHub's DCO app asks for action when a commit is not signed off: that is a
    # check that did not pass, not a workflow waiting for a maintainer's approval.
    dco = CheckRun(
        id=9,
        name="DCO",
        status=CheckStatus.COMPLETED,
        conclusion=CheckConclusion.ACTION_REQUIRED,
    )
    assert not dco.awaiting_approval
    held = _job(10, "unit", None, 77, workflow="CI").model_copy(
        update={
            "status": CheckStatus.QUEUED,
            "conclusion": CheckConclusion.ACTION_REQUIRED,
        }
    )
    assert held.awaiting_approval
    pr = make_pr(
        reviews=[Review(id=1, author="bob", state=ReviewEventState.APPROVED)],
        checks=[dco, _job(11, "lint", "success", 78, workflow="Lint")],
    )
    info = RepoInfo(name=REPO, required_checks=["DCO", "lint"], required_approvals=1)

    def outcome(profile):  # type: ignore[no-untyped-def]
        return evaluate_merge_gate(
            pr, profile, info, None, {"bob": Permission.WRITE}, set(), {}, False, T0
        )

    strict = RepoProfile(repo=REPO)
    assert any(i.name == "check:DCO" and not i.ok for i in outcome(strict).items)
    lenient = RepoProfile.model_validate(
        {"repo": REPO, "ci": {"start": {"optional_checks": ["DCO"]}}}
    )
    unpassed = [
        i for i in outcome(lenient).items if i.name.startswith("check:") and not i.ok
    ]
    assert unpassed == []
    assert ci_view(pr, None, lenient.ci.start).state == "passed"


def test_only_a_base_merge_by_the_scheduler_keeps_the_schedule() -> None:
    armed = Commit(sha="a" * 40, message="fix: x", author_login="alice")
    merge = Commit(
        sha="b" * 40,
        message="Merge branch 'main' into fix-x",
        author_login="maria",
    )
    pr = make_pr(sha="b" * 40, commits=[armed, merge])
    assert only_base_merged(pr, "a" * 40, {"maria", "am-bot"})
    assert not only_base_merged(pr, "a" * 40, {"am-bot"}), "someone else merged it"
    pushed = Commit(sha="c" * 40, message="more", author_login="alice")
    assert not only_base_merged(
        pr.model_copy(update={"commits": [armed, merge, pushed], "head_sha": "c" * 40}),
        "a" * 40,
        {"maria"},
    )


# -- the flow --------------------------------------------------------------------------


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    # Every action stays at its default level, shadow: what the schedule does, it
    # does because a maintainer scheduled the PR.
    h = await Harness.create(
        tmp_path,
        layer={
            "extends": ["github-actions"],
            "merge": {"enabled": True},
            "ci": {
                "required_workflows": ["CI"],
                "start": {"gate_jobs": ["check-changes"], "start_minutes": 20},
            },
            "labels": {"roles": {"ci_gate": "run-ci"}},
        },
    )
    h.forge.set_permission(REPO, "bob", Permission.WRITE)
    await h.service.a.user_tokens.set("maria", UserToken(token="m"))
    yield h
    await h.close()


def _ready(number: int, sha: str, checks: list[CheckRun], **kw: Any):
    return make_pr(
        number=number,
        sha=sha,
        title=f"change {number}",
        labels=kw.pop("labels", ["run-ci"]),
        files=[PrFile(path=f"src/m{number}.py", additions=5)],
        commits=[Commit(sha=sha, message=f"change {number}", author_login="alice")],
        reviews=[
            Review(
                id=number,
                author="bob",
                state=ReviewEventState.APPROVED,
                commit_sha=sha,
            )
        ],
        checks=checks,
        **kw,
    )


def _ci(number: int, conclusion: str | None, run: int | None = None) -> list[CheckRun]:
    return [_job(number * 10, "unit", conclusion, run or number * 100, workflow="CI")]


async def _schedule(h: Harness, number: int, by: str = "maria", **kw: Any) -> None:
    pr = await h.forge.get_pr(REPO, number)
    assert pr is not None
    at = h.clock.now()
    await h.store.save_arm(
        MergeArm(
            repo=REPO,
            number=number,
            sha=pr.head_sha,
            armed_by=by,
            armed_at=at,
            armer_permission=Permission.ADMIN,
            position=float(number),
            **kw,
        )
    )
    await h.service.bus.publish(
        Event(
            kind=EventKind.MERGE_ARMED,
            repo=REPO,
            number=number,
            payload={"by": by, "armed_at": at.isoformat()},
        )
    )


async def _phase(h: Harness, number: int) -> ScheduleState:
    return ScheduleState.model_validate(
        await h.store.get_state(state_key(REPO, number)) or {}
    )


async def test_scheduled_prs_merge_one_at_a_time_in_the_maintainers_order(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    h.forge.put_pr(_ready(43, "c" * 40, _ci(43, "success")))
    merge = h.forge.merge

    async def merge_and_move_main(repo, number, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = await merge(repo, number, *args, **kwargs)
        for (r, n), pr in list(h.forge.prs.items()):
            if r == repo and n != number and pr.state == PrState.OPEN:
                h.forge.put_pr(pr.model_copy(update={"behind_base": True}))
        return out

    h.forge.merge = merge_and_move_main  # type: ignore[method-assign]
    await _schedule(h, 42)
    await _schedule(h, 43)
    await h.settle()
    # 42 merges first; merging puts 43 behind main, which it must be up to date with.
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]
    [update] = h.forge.writes_of("update_branch")
    assert update["number"] == 43
    assert update["author"] == "maria", (
        "a base merge is a commit, made as the scheduler"
    )
    assert (await _phase(h, 43)).phase == "updating"

    # The base merge lands; 43's CI runs again at the new head and passes.
    pr = await h.forge.get_pr(REPO, 43)
    merged_in = Commit(
        sha="e" * 40, message="Merge branch 'main' into x", author_login="maria"
    )
    h.forge.put_pr(
        pr.model_copy(
            update={
                "head_sha": "e" * 40,
                "behind_base": False,
                "commits": [*pr.commits, merged_in],
                "checks": _ci(43, "success", run=4301),
            }
        )
    )
    await h.emit(
        Event(kind=EventKind.PR_SYNCHRONIZED, repo=REPO, number=43, sha="e" * 40)
    )
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42, 43]
    merged = await h.store.get_state(f"merge-schedule-merged:{REPO}")
    assert [m["number"] for m in merged] == [43, 42]


async def test_scheduling_starts_ci_with_the_gate_label_and_reruns_a_flake(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, [], labels=[]))
    await _schedule(h, 42)
    await h.settle()
    [label] = [w for w in h.forge.writes_of("add_labels") if w["number"] == 42]
    assert label["labels"] == ["run-ci"]
    assert (await _phase(h, 42)).phase == "starting"

    # CI runs and a job flakes on the network: the schedule reruns it.
    pr = await h.forge.get_pr(REPO, 42)
    h.forge.job_logs[420] = "npm ERR! network ECONNRESET while fetching\n"
    h.forge.put_pr(
        pr.model_copy(update={"labels": ["run-ci"], "checks": _ci(42, "failure")})
    )
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert h.forge.writes_of("rerun_failed_jobs") == [{"repo": REPO, "run_id": 4200}]
    assert (await _phase(h, 42)).phase == "rerunning"
    [rerun] = await h.store.list_actions(repo=REPO, kinds=[ActionKind.CI_RERUN_FAILED])
    assert rerun.status == ActionStatus.EXECUTED and rerun.approver == "maria"


async def test_a_real_failure_goes_to_the_failure_lane_and_the_next_pr_goes(
    harness: Harness,
) -> None:
    h = harness
    h.runner.on(
        "ci_diagnose",
        lambda t: {
            "category": "pr_caused",
            "confidence": 0.8,
            "explanation": "it breaks",
        },
    )
    h.runner.on(
        "ci_root_cause",
        lambda t: {
            "causes": [
                {
                    "jobs": ["unit"],
                    "failing": "tests/test_m.py::test_x",
                    "origin": "change",
                    "cause": "The new branch returns None.",
                    "confidence": 0.8,
                }
            ]
        },
    )
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    h.forge.put_pr(_ready(43, "c" * 40, _ci(43, "success")))
    await _schedule(h, 42)
    await _schedule(h, 43)
    await h.settle()
    failed = await _phase(h, 42)
    assert failed.phase == "failed" and failed.failed_jobs == ["unit"]
    assert any("The new branch returns None." in c for c in failed.causes)
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43]


async def test_a_pr_waits_for_approvals_after_its_ci_without_holding_the_turn(
    tmp_path: Path,
) -> None:
    # A repository whose reviewers approve once CI passed schedules before them.
    h = await Harness.create(
        tmp_path,
        layer={
            "extends": ["github-actions"],
            "merge": {
                "enabled": True,
                "schedule": {"ready": ["not_draft", "no_changes_requested"]},
            },
            "ci": {"required_workflows": ["CI"]},
            "labels": {"roles": {"ci_gate": "run-ci"}},
        },
    )
    h.forge.set_permission(REPO, "bob", Permission.WRITE)
    await h.service.a.user_tokens.set("maria", UserToken(token="m"))
    unapproved = _ready(42, "a" * 40, _ci(42, "success")).model_copy(
        update={"reviews": []}
    )
    h.forge.put_pr(unapproved)
    h.forge.put_pr(_ready(43, "c" * 40, _ci(43, "success")))
    merge = h.forge.merge

    async def merge_and_move_main(repo, number, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = await merge(repo, number, *args, **kwargs)
        for (r, n), pr in list(h.forge.prs.items()):
            if r == repo and n != number and pr.state == PrState.OPEN:
                h.forge.put_pr(pr.model_copy(update={"behind_base": True}))
        return out

    h.forge.merge = merge_and_move_main  # type: ignore[method-assign]
    await _schedule(h, 42)
    await _schedule(h, 43)
    await h.settle()
    # 42's CI passed but nobody approved it: 43 takes the turn and merges.
    waiting = await _phase(h, 42)
    assert waiting.phase == "approval" and "approvals" in waiting.detail
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43]
    assert not h.forge.writes_of("update_branch"), "it does not chase main meanwhile"

    # Approved now: it takes a turn, merges main in, its CI passes again, it merges.
    pr = await h.forge.get_pr(REPO, 42)
    approved = Review(
        id=8, author="bob", state=ReviewEventState.APPROVED, commit_sha="a" * 40
    )
    h.forge.put_pr(pr.model_copy(update={"reviews": [approved]}))
    await h.emit(Event(kind=EventKind.PR_REVIEW_SUBMITTED, repo=REPO, number=42))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("update_branch")] == [42]
    pr = await h.forge.get_pr(REPO, 42)
    merged_in = Commit(
        sha="e" * 40, message="Merge branch 'main' into x", author_login="maria"
    )
    h.forge.put_pr(
        pr.model_copy(
            update={
                "head_sha": "e" * 40,
                "behind_base": False,
                "commits": [*pr.commits, merged_in],
                "checks": _ci(42, "success", run=4201),
            }
        )
    )
    await h.emit(
        Event(kind=EventKind.PR_SYNCHRONIZED, repo=REPO, number=42, sha="e" * 40)
    )
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43, 42]
    await h.close()


async def test_the_repositorys_target_can_be_set_on_the_page(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    unapproved = _ready(42, "a" * 40, []).model_copy(update={"reviews": []})
    h.forge.put_pr(unapproved)
    h.forge.put_pr(_ready(43, "c" * 40, [], behind_base=True))
    await h.poll()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert [c["number"] for c in view["candidates"]] == [43], "behind main is fine"
        assert view["rules"]["ready_set"] is False
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/ready",
                json={"ready": ["not_draft", "no_conflict"]},
            )
        ).json()
        assert out["applied"], out
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert [c["number"] for c in view["candidates"]] == [42, 43]
        assert view["rules"]["ready"] == ["not_draft", "no_conflict"]
        bad = await maria.put(
            f"/api/merge-schedule/{REPO}/ready", json={"ready": ["passes_ci"]}
        )
        assert bad.status_code == 422


async def test_ci_that_does_not_start_is_looked_into_once(harness: Harness) -> None:
    h = harness
    looks: list[str] = []

    def stalled(task):  # type: ignore[no-untyped-def]
        looks.append(task.prompt)
        return {
            "cause": "A required status from the docs service never reported.",
            "step": "maintainer",
            "for_maintainer": "Check the docs build.",
        }

    h.runner.on("ci_diagnose", stalled)
    h.forge.put_pr(_ready(42, "a" * 40, []))
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "starting" and not looks
    h.clock.advance(minutes=25)
    await h.service.engine.tick()
    await h.settle()
    assert len(looks) == 1 and "has not started" in looks[0]
    state = await _phase(h, 42)
    assert "never reported" in state.finding
    h.clock.advance(minutes=25)
    await h.service.engine.tick()
    await h.settle()
    assert len(looks) == 1, "once per head"


async def test_a_pr_held_back_waits_without_its_turn(harness: Harness) -> None:
    h = harness
    pr = _ready(42, "a" * 40, _ci(42, "success"))
    h.forge.put_pr(
        pr.model_copy(
            update={
                "reviews": [
                    *pr.reviews,
                    Review(
                        id=9, author="carol", state=ReviewEventState.CHANGES_REQUESTED
                    ),
                ]
            }
        )
    )
    h.forge.put_pr(_ready(43, "c" * 40, _ci(43, "success")))
    await _schedule(h, 42)
    await _schedule(h, 43)
    await h.settle()
    held = await _phase(h, 42)
    assert held.phase == "held" and "carol" in held.detail
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43]


async def test_scheduling_a_pr_again_leaves_one_job_driving_it(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, None)))
    await _schedule(h, 42)
    await h.settle()
    # Unscheduled and scheduled again before the first job wakes to its unscheduling.
    await h.store.delete_arm(REPO, 42)
    await h.service.bus.publish(
        Event(kind=EventKind.MERGE_DISARMED, repo=REPO, number=42)
    )
    h.clock.advance(minutes=1)
    await _schedule(h, 42)
    await h.settle()
    jobs = [j for j in await h.store.list_jobs() if j.playbook == "merge"]
    first = min(jobs, key=lambda j: j.created_at)
    assert first.result == {"done": "scheduled again since"}, first.result
    assert (await _phase(h, 42)).phase == "running", "the new schedule's state stays"
    pr = await h.forge.get_pr(REPO, 42)
    h.forge.put_pr(pr.model_copy(update={"checks": _ci(42, "success")}))
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    merges = await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_MERGE])
    assert len(merges) == 1, "one job drives the PR"


async def test_the_schedule_approves_only_its_schedulers_steps_at_the_armed_head(
    harness: Harness,
) -> None:
    from auto_maintainer.domain.actions import RerunPayload, make_action

    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await h.store.save_arm(
        MergeArm(repo=REPO, number=42, sha="a" * 40, armed_by="maria", armed_at=T0)
    )

    def rerun(by: str, sha: str):  # type: ignore[no-untyped-def]
        action = make_action(
            ActionKind.CI_RERUN_FAILED,
            REPO,
            RerunPayload(run_id=4200),
            number=42,
            head_sha=sha,
            discriminator=f"t:{by}:{sha[:3]}",
            draft_for=by,
        )
        action.provenance.scheduled_by = by
        return action

    walt = await h.service.pipeline.submit(rerun("walt", "a" * 40))
    stale = await h.service.pipeline.submit(rerun("maria", "f" * 40))
    assert walt.status == stale.status == ActionStatus.PROPOSED
    ok = await h.service.pipeline.submit(rerun("maria", "a" * 40))
    assert ok.status in (ActionStatus.QUEUED, ActionStatus.EXECUTED)


async def test_automatic_scheduling_arms_ready_prs_as_the_maintainer_who_chose_it(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        layer={
            "merge": {
                "enabled": True,
                "schedule": {"auto": True, "auto_by": "maria"},
            },
            "ci": {"required_workflows": ["CI"]},
        },
    )
    try:
        h.forge.set_permission(REPO, "bob", Permission.WRITE)
        held = [
            _job(1, "unit", None, 7, workflow="CI").model_copy(
                update={
                    "status": CheckStatus.QUEUED,
                    "conclusion": CheckConclusion.ACTION_REQUIRED,
                }
            )
        ]
        h.forge.put_pr(_ready(42, "a" * 40, held, labels=[]))
        h.forge.put_pr(_ready(44, "b" * 40, [], labels=[], is_draft=True))
        await h.emit(Event(kind=EventKind.PR_RECONCILED, repo=REPO, number=42))
        await h.emit(Event(kind=EventKind.PR_RECONCILED, repo=REPO, number=44))
        await h.settle()
        arm = await h.store.get_arm(REPO, 42)
        assert arm is not None and arm.armed_by == "maria" and arm.auto
        assert await h.store.get_arm(REPO, 44) is None, "a draft is not ready"
        # The fork's workflows wait for a person: automatic scheduling never saw it.
        assert not h.forge.writes_of("approve_workflow_run")
        state = await _phase(h, 42)
        assert state.approval_needed
    finally:
        await h.close()


# -- the page ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_the_page_lists_candidates_and_only_maintainers_schedule(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    h.forge.put_pr(
        make_pr(
            number=45,
            sha="9" * 40,
            title="changes asked for",
            reviews=[
                Review(id=7, author="bob", state=ReviewEventState.CHANGES_REQUESTED)
            ],
        )
    )
    await h.poll()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        view = (await bob.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert [c["number"] for c in view["candidates"]] == [42]
        [waiting] = [r for r in view["not_ready"] if r["number"] == 45]
        assert any(
            "changes requested" in r or "approvals" in r for r in waiting["reasons"]
        )
        assert view["can_arrange"] is False and view["mode"] == "serial"
        refused = await bob.post(
            f"/api/merge-schedule/{REPO}/schedule", json={"number": 42}
        )
        assert refused.status_code == 403
    async with _as(app, "maria") as maria:
        not_ready = await maria.post(
            f"/api/merge-schedule/{REPO}/schedule", json={"number": 45}
        )
        assert not_ready.status_code == 409 and "not ready" in not_ready.text
        done = await maria.post(
            f"/api/merge-schedule/{REPO}/schedule", json={"number": 42}
        )
        assert done.status_code == 200
        await h.settle()
        assert [w["number"] for w in h.forge.writes_of("merge")] == [42]
        assert (await maria.delete(f"/api/merge-schedule/{REPO}/42")).status_code == 404


async def test_turning_automatic_scheduling_on_records_who_did(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/settings",
                json={"auto": True, "parallel": 3},
            )
        ).json()
        assert out["applied"], out
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    assert profile.merge.schedule.auto and profile.merge.schedule.auto_by == "maria"
    assert profile.merge.schedule.parallel == 3
    async with _as(app, "bob") as bob:
        proposed = (
            await bob.put(f"/api/merge-schedule/{REPO}/settings", json={"auto": False})
        ).json()
        assert not proposed["applied"], "only a maintainer changes it"


async def test_merge_scheduling_is_on_by_default_and_only_maintainers_schedule(
    tmp_path: Path,
) -> None:
    from auto_maintainer.domain.snapshot import IssueComment

    h = await Harness.create(tmp_path)
    try:
        profile = await h.service.profiles.active(REPO)
        assert profile is not None and profile.merge.enabled
        assert not profile.merge.schedule.auto, "automatic scheduling stays off"
        h.forge.set_permission(REPO, "walt", Permission.WRITE)
        h.forge.set_permission(REPO, "bob", Permission.WRITE)
        h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
        await h.poll()
        pr = await h.forge.get_pr(REPO, 42)
        assert pr is not None
        asked = IssueComment(id=5, author="walt", body="/am merge")
        h.forge.put_pr(pr.model_copy(update={"comments": [asked]}))
        await h.poll()
        assert await h.store.get_arm(REPO, 42) is None, "a writer does not schedule"
        replies = [
            a.action.payload["body"]
            for a in await h.store.list_actions(
                repo=REPO, kinds=[ActionKind.COMMENT_UPSERT]
            )
        ]
        assert any("needs a maintainer" in r for r in replies), replies
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with _as(app, "walt") as walt:
            out = await walt.post(f"/api/prs/{REPO}/42/merge")
            assert out.status_code == 403
        async with _as(app, "maria") as maria:
            assert (await maria.post(f"/api/prs/{REPO}/42/merge")).status_code == 200
        arm = await h.store.get_arm(REPO, 42)
        assert arm is not None and arm.armed_by == "maria"
    finally:
        await h.close()


def test_an_agents_draft_keeps_only_what_the_repository_has() -> None:
    from auto_maintainer.playbooks.ci_rules import CiRulesDraft, checked

    workflows = (
        "jobs:\n  check-changes:\n    runs-on: x\n  ci-test-finish:\n    runs-on: x\n"
    )
    draft = CiRulesDraft(
        how="CI starts on the run-ci label.",
        gate_label="run-ci",
        gate_jobs=["check-changes", "made-up"],
        summary_jobs=["ci-test-finish"],
        ready=["approvals", "threads_resolved", "passes_ci"],
        optional_checks=["DCO", "lint-but-not-required"],
    )
    kept, dropped = checked(draft, workflows, ["run-ci"], ["DCO", "ci-test-finish"])
    assert kept.ready == ["approvals", "threads_resolved"]
    assert kept.optional_checks == ["DCO"]
    assert kept.gate_jobs == ["check-changes"]
    assert len(dropped) == 3


async def test_a_maintainer_chooses_which_labels_hold_a_merge_back(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert view["rules"]["label_roles"]["ci_gate"] == "run-ci"
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/blocking", json={"roles": ["ci_gate"]}
            )
        ).json()
        assert out["applied"], out
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert view["rules"]["blocking_labels"] == ["run-ci"]
        out = (
            await maria.put(f"/api/merge-schedule/{REPO}/blocking", json={"roles": []})
        ).json()
        assert out["applied"], out
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert view["rules"]["blocking_labels"] == []
        bad = await maria.put(
            f"/api/merge-schedule/{REPO}/blocking", json={"roles": ["nonsense"]}
        )
        assert bad.status_code == 422
    async with _as(app, "bob") as bob:
        proposed = (
            await bob.put(
                f"/api/merge-schedule/{REPO}/blocking",
                json={"roles": ["ci_gate"]},
            )
        ).json()
        assert proposed["applied"] is False, proposed
        assert proposed["change"]["state"] == "open", "a proposal for the maintainers"


async def test_checking_all_prs_reads_each_from_the_forge_again(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, []))
    h.forge.put_pr(_ready(43, "c" * 40, []))
    await h.poll()
    reads: list[int] = []
    seen: list[dict[str, int] | None] = []
    refresh = h.service._refresh_pr

    async def counted(repo: str, number: int) -> None:
        # How far the check got, as the page shows it, before each PR is read.
        seen.append(h.service.check_progress(repo))
        reads.append(number)
        await refresh(repo, number)

    h.service._refresh_pr = counted
    # GitHub counts an approval a moment late: the stored PR still waits for it.
    pr = await h.forge.get_pr(REPO, 42)
    h.forge.put_pr(pr.model_copy(update={"review_decision": "APPROVED"}))
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        out = (await bob.post(f"/api/merge-schedule/{REPO}/check")).json()
        assert out == {"started": True, "prs": 2}
        again = (await bob.post(f"/api/merge-schedule/{REPO}/check")).json()
        assert again["started"] is False, "one check at a time"
        await h.service._checks[REPO]
        view = (await bob.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert view["checking"] is False and view["check_progress"] is None
    assert sorted(reads) == [42, 43]
    assert seen == [{"done": 0, "total": 2}, {"done": 1, "total": 2}]
    stored = await h.store.get_snapshot(REPO, 42)
    assert stored is not None and stored.review_decision == "APPROVED"


async def test_rules_described_in_words_become_a_draft_a_maintainer_saves(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    asked: list[str] = []

    def parse(task):  # type: ignore[no-untyped-def]
        asked.append(task.prompt)
        assert task.budget == "quick" and task.mode == "read_only"
        return {
            "how": "",
            "gate_label": "run-ci",
            "gate_jobs": ["check-changes"],
            "summary_jobs": [],
            "start_minutes": 20,
            "run_minutes": 180,
            "ready": ["approvals", "threads_resolved"],
            "blocking_roles": [],
            "optional_checks": ["DCO", "made-up"],
            "diagnose": "patterns",
            "max_reruns": 2,
            "backoff_seconds": 120,
            "rerun_unknown": 1,
            "root_cause": False,
            "flake_patterns": [
                {
                    "id": "hub",
                    "pattern": "huggingface.co.*timed out",
                    "category": "flaky",
                },
                {"id": "broken", "pattern": "(unclosed", "category": "flaky"},
            ],
            "merge_notes": "",
            "approvals": 0,
            "merge_method": "squash",
            "preserve_trailers": ["Signed-off-by", "not a trailer!"],
            "required_workflows": ["CI", "Nope"],
            "parallel": 1,
            "update_branch": True,
        }

    h.runner.on("onboard", parse)
    h.forge.add_repo(
        RepoInfo(
            name=REPO,
            labels=["run-ci", "waiting on author"],
            required_checks=["DCO", "lint"],
            default_branch_sha="d" * 40,
        )
    )
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        words = (
            "Schedulable once approved with every comment resolved; DCO may fail; "
            "judge flakes by patterns only and rerun anything else once."
        )
        out = await maria.post(
            f"/api/merge-schedule/{REPO}/describe", json={"text": words}
        )
        assert out.status_code == 200, out.text
        await h.settle()
        assert asked and words in asked[0] and "Current settings" in asked[0]
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        draft = view["draft"]
        assert draft["source"] == "words" and draft["words"] == words
        assert draft["draft"]["optional_checks"] == ["DCO"]
        assert [p["id"] for p in draft["draft"]["flake_patterns"]] == ["hub"]
        assert len(draft["dropped"]) == 4, draft["dropped"]
        assert draft["draft"]["preserve_trailers"] == ["Signed-off-by"]
        assert draft["draft"]["required_workflows"] == ["CI"]
        saved = (await maria.post(f"/api/merge-schedule/{REPO}/draft/apply")).json()
        assert saved["applied"], saved
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    assert profile.merge.schedule.ready == ["approvals", "threads_resolved"]
    assert profile.merge.blocking_roles == []
    assert profile.ci.start.optional_checks == ["DCO"]
    assert profile.ci.diagnose == "patterns" and profile.ci.rerun.unknown == 1
    assert profile.ci.root_cause is False
    assert "hub" in [s.id for s in profile.ci.flake_signatures]
    assert profile.merge.message.preserve_trailers == ["Signed-off-by"]
    assert profile.merge.schedule.parallel == 1
    assert await h.store.get_state(f"ci-rules:{REPO}") is None, "saved, so gone"


async def test_a_writer_cannot_have_the_rules_drafted(
    harness: Harness, tmp_path: Path
) -> None:
    app = create_app(harness.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        out = await bob.post(
            f"/api/merge-schedule/{REPO}/describe", json={"text": "merge anything"}
        )
        assert out.status_code == 403


async def test_with_patterns_only_no_agent_reads_a_log_and_other_failures_rerun_first(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        layer={
            "merge": {"enabled": True},
            "ci": {
                "required_workflows": ["CI"],
                "diagnose": "patterns",
                "root_cause": False,
                "rerun": {"max_per_sha": 2, "unknown": 1},
            },
        },
    )
    try:
        h.forge.set_permission(REPO, "bob", Permission.WRITE)
        await h.service.a.user_tokens.set("maria", UserToken(token="m"))
        h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
        h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
        await _schedule(h, 42)
        await h.settle()
        assert not [t for t in h.runner.tasks if str(t.kind) == "ci_diagnose"]
        # Nothing calls it a flake, but it gets one rerun before it counts.
        assert h.forge.writes_of("rerun_failed_jobs") == [
            {"repo": REPO, "run_id": 4200}
        ]
        assert (await _phase(h, 42)).phase == "rerunning"
        # The rerun fails the same way: now it is real.
        pr = await h.forge.get_pr(REPO, 42)
        again = _job(421, "unit", "failure", 4200, workflow="CI", attempt=2)
        h.forge.job_logs[421] = "FAILED tests/test_m.py::test_x\n"
        h.forge.put_pr(pr.model_copy(update={"checks": [again]}))
        await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
        await h.settle()
        assert (await _phase(h, 42)).phase == "failed"
        assert len(h.forge.writes_of("rerun_failed_jobs")) == 1
        assert not [t for t in h.runner.tasks if str(t.kind) == "ci_root_cause"]
    finally:
        await h.close()


async def test_a_maintainer_sets_how_flakes_are_handled_on_the_page(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/flaky",
                json={
                    "diagnose": "patterns",
                    "max_reruns": 3,
                    "backoff_seconds": 60,
                    "rerun_unknown": 1,
                    "root_cause": True,
                },
            )
        ).json()
        assert out["applied"], out
        bad = await maria.put(
            f"/api/merge-schedule/{REPO}/flake-patterns",
            json={"patterns": [{"id": "x", "pattern": "(oops"}]},
        )
        assert bad.status_code == 422
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/flake-patterns",
                json={"patterns": [{"id": "hub", "pattern": "timed out"}]},
            )
        ).json()
        assert out["applied"], out
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        flaky = view["flaky"]
        assert flaky["diagnose"] == "patterns" and flaky["max_reruns"] == 3
        own = [p for p in flaky["patterns"] if p["own"]]
        assert [p["id"] for p in own] == ["hub"]
        assert any(not p["own"] for p in flaky["patterns"]), "a pack's stay listed"


async def test_the_page_shows_a_diagnosis_at_work_with_its_job(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    seen: list[dict] = []
    h.runner.on(
        "ci_diagnose",
        lambda t: {"category": "pr_caused", "confidence": 0.8, "explanation": "breaks"},
    )

    async def root_cause(task):  # type: ignore[no-untyped-def]
        async with _as(app, "bob") as bob:
            view = (await bob.get(f"/api/merge-schedule/{REPO}/view")).json()
        seen.append(view["scheduled"][0])
        return {"causes": []}

    h.runner.on("ci_root_cause", root_cause)
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await h.poll()
    await _schedule(h, 42)
    await h.settle()
    [during] = seen
    assert during["phase"] == "diagnosing"
    assert "root cause of unit" in during["detail"]
    assert during["job"] and during["job"]["status"] == "running"
    assert (await _phase(h, 42)).phase == "failed"


async def test_the_pr_page_reads_ci_as_the_schedule_and_github_do(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    # A label started a second run after the first stopped at the gate; GitHub shows
    # the newest, and so do the PR page and the schedule.
    old = _job(1, "check-changes", "failure", 100, workflow="CI")
    new = [
        _job(2, "check-changes", "success", 101, workflow="CI"),
        _job(3, "unit", None, 101, workflow="CI"),
    ]
    h.forge.put_pr(_ready(42, "a" * 40, [old, *new]))
    await h.poll()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        detail = (await bob.get(f"/api/prs/{REPO}/42")).json()
        view = (await bob.get(f"/api/merge-schedule/{REPO}/view")).json()
    status = detail["ci_status"]
    assert status["state"] == "running" and status["failed"] == []
    assert sorted(status["latest"]) == [2, 3]
    [row] = [r for r in view["candidates"] + view["not_ready"] if r["number"] == 42]
    assert row["ci"]["state"] == status["state"]


async def test_a_failure_the_root_cause_lays_on_the_runner_is_rerun_not_failed(
    harness: Harness,
) -> None:
    h = harness
    # The quick look cannot tell; the look at the code finds the runner broke.
    h.runner.on(
        "ci_diagnose",
        lambda t: {"category": "unknown", "confidence": 0.3, "explanation": "unclear"},
    )
    causes: list[str] = []

    def root_cause(task):  # type: ignore[no-untyped-def]
        causes.append(task.prompt)
        return {
            "causes": [
                {
                    "jobs": ["unit"],
                    "failing": "the job's container",
                    "origin": "infra",
                    "cause": "The runner ran out of disk while pulling the image.",
                    "confidence": 0.8,
                }
            ]
        }

    h.runner.on("ci_root_cause", root_cause)
    h.forge.job_logs[420] = "Error: write /var/lib/docker: no space left\n"
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await _schedule(h, 42)
    await h.settle()
    assert h.forge.writes_of("rerun_failed_jobs") == [{"repo": REPO, "run_id": 4200}]
    state = await _phase(h, 42)
    assert state.phase == "rerunning", state.detail
    assert len(causes) == 1, "the root cause is looked for once"
    [observation] = [
        o for o in await h.store.list_ci_observations(REPO) if o.check_id == 420
    ]
    assert observation.category == "infra", (
        "kept, so the same failure is known next time"
    )


async def test_retry_runs_what_failed_again(harness: Harness, tmp_path: Path) -> None:
    h = harness
    h.runner.on(
        "ci_diagnose",
        lambda t: {"category": "unknown", "confidence": 0.5, "explanation": "unclear"},
    )
    h.runner.on("ci_root_cause", lambda t: {"causes": []})
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await h.poll()
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "failed"
    assert not h.forge.writes_of("rerun_failed_jobs")
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = await maria.post(f"/api/merge-schedule/{REPO}/42/retry")
        assert out.status_code == 200, out.text
    await h.settle()
    # The maintainer's retry runs it again, whatever the first look said.
    assert h.forge.writes_of("rerun_failed_jobs") == [{"repo": REPO, "run_id": 4200}]
    state = await _phase(h, 42)
    assert state.phase == "rerunning" and not state.retry


async def test_a_failure_marked_flaky_is_rerun_after_and_teaches_a_pattern(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    looks: list[str] = []

    def agent(task):  # type: ignore[no-untyped-def]
        looks.append(task.activity or "")
        if "marked a failed CI job flaky" in task.prompt:
            return {
                "id": "zmq-port-in-use",
                "pattern": r"ZMQError: Address already in use",
                "category": "flaky",
                "description": "SGLang could not bind its ZMQ port on a shared runner.",
            }
        return {"category": "unknown", "confidence": 0.5, "explanation": "unclear"}

    h.runner.on("ci_diagnose", agent)
    h.runner.on("ci_root_cause", lambda t: {"causes": []})
    log = (
        "Traceback\nzmq.error.ZMQError: Address already in use (addr='tcp://*:5557')\n"
    )
    h.forge.job_logs[420] = log
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await h.poll()
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "failed"
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = (await maria.post(f"/api/merge-schedule/{REPO}/42/flaky")).json()
        assert out["marked"] == 1
        await h.settle()
        # It runs again at once, and an agent learned a pattern from the log.
        assert h.forge.writes_of("rerun_failed_jobs") == [
            {"repo": REPO, "run_id": 4200}
        ]
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        [suggested] = view["flaky"]["suggestions"]
        assert suggested["id"] == "zmq-port-in-use" and suggested["by"] == "maria"
    [judged] = [
        o for o in await h.store.list_ci_observations(REPO) if o.check_id == 420
    ][:1]
    assert judged.verdict == "flaky" and judged.verdict_by == "maria"

    # The same failure on another run: taken as flaky with no agent's look.
    looks.clear()
    pr = await h.forge.get_pr(REPO, 42)
    again = _job(422, "unit", "failure", 4200, workflow="CI", attempt=2)
    h.forge.job_logs[422] = log.replace("5557", "5561")
    h.forge.put_pr(pr.model_copy(update={"checks": [again]}))
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert looks == [], "a person's word needs no agent's look"
    assert (await _phase(h, 42)).phase == "rerunning"


async def test_a_learned_pattern_must_match_its_log_and_no_ordinary_failure() -> None:
    from auto_maintainer.playbooks.flake_learn import Suggested, usable

    log = "ConnectionResetError: [Errno 104] Connection reset by peer while downloading"
    assert (
        usable(Suggested(id="reset", pattern=r"Connection reset by peer"), log) is None
    )
    assert "does not match" in (
        usable(Suggested(id="quota", pattern=r"Disk quota"), log) or ""
    )
    assert "ordinary failure" in (
        usable(Suggested(id="broad", pattern=r"Error|reset by peer"), log) or ""
    )
    assert "compile" in (
        usable(Suggested(id="broken", pattern=r"(unclosed group"), log) or ""
    )


async def test_verdicts_are_recorded_by_people_who_see_the_repository(
    harness: Harness, tmp_path: Path
) -> None:
    from auto_maintainer.domain.records import CiObservation

    h = harness
    obs = CiObservation(
        repo=REPO,
        number=42,
        sha="a" * 40,
        check_id=1,
        check_name="unit",
        category="unknown",
    )
    await h.store.add_ci_observation(obs)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        out = (
            await bob.put(
                f"/api/merge-schedule/{REPO}/verdicts/{obs.id}",
                json={"verdict": "real"},
            )
        ).json()
        assert out["verdict"] == "real" and out["verdict_by"] == "bob"
        back = (
            await bob.put(
                f"/api/merge-schedule/{REPO}/verdicts/{obs.id}", json={"verdict": None}
            )
        ).json()
        assert back["verdict"] is None
        missing = await bob.put(
            f"/api/merge-schedule/{REPO}/verdicts/ci_nope", json={"verdict": "flaky"}
        )
        assert missing.status_code == 404


def test_the_owners_approval_counts_when_the_service_acts_as_them() -> None:
    from auto_maintainer.domain.merge_gate import evaluate_merge_gate
    from auto_maintainer.domain.profile import RepoProfile

    profile = RepoProfile(repo=REPO)
    info = RepoInfo(name=REPO, required_approvals=2)

    def approvals(*authors: str) -> str:
        pr = make_pr(
            reviews=[
                Review(id=i, author=a, state=ReviewEventState.APPROVED)
                for i, a in enumerate(authors, 1)
            ]
        )
        perms = dict.fromkeys(authors, Permission.ADMIN)
        gate = evaluate_merge_gate(
            pr, profile, info, None, perms, {"maria", "am-bot[bot]"}, {}, False, T0
        )
        return next(i.detail for i in gate.items if i.name == "approvals")

    # The deployment acts on GitHub as its owner maria: her approval is hers.
    assert approvals("maria", "bob").startswith("2 of 2")
    # A bot's never counts, the service's own bot account among them.
    assert approvals("am-bot[bot]", "bob").startswith("1 of 2")
    assert approvals("dependabot[bot]", "bob").startswith("1 of 2")


def test_the_light_poll_notices_a_resolved_conversation() -> None:
    from auto_maintainer.forges.github.reader import _light_fingerprint

    def node(resolved: list[bool], decision: str | None) -> dict:
        return {
            "number": 42,
            "updatedAt": "2026-11-12T11:19:50Z",
            "headRefOid": "a" * 40,
            "reviewDecision": decision,
            "reviewThreads": {
                "totalCount": len(resolved),
                "nodes": [{"isResolved": r} for r in resolved],
            },
            "commits": {"nodes": []},
        }

    before = _light_fingerprint(node([True, False], "REVIEW_REQUIRED"))
    # GitHub keeps the update time when a conversation is resolved.
    assert _light_fingerprint(node([True, True], "REVIEW_REQUIRED")) != before
    assert _light_fingerprint(node([True, False], "APPROVED")) != before


async def test_a_pr_scheduled_by_hand_runs_ci_then_waits_for_the_merge_rules(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    unapproved = _ready(42, "a" * 40, _ci(42, "success")).model_copy(
        update={"reviews": []}
    )
    h.forge.put_pr(unapproved)
    await h.poll()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        refused = await maria.post(
            f"/api/merge-schedule/{REPO}/schedule", json={"number": 42}
        )
        assert refused.status_code == 409
        out = await maria.post(
            f"/api/merge-schedule/{REPO}/schedule", json={"number": 42, "force": True}
        )
        assert out.status_code == 200, out.text
        assert out.json()["forced"] is True
        await h.settle()
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
    [row] = view["scheduled"]
    assert row["forced"] and row["phase"] == "approval", row
    assert "approvals" in row["detail"]
    assert not h.forge.writes_of("merge"), "it merges only once the rules hold"


async def test_a_draft_from_an_older_version_shows_and_saves_only_what_it_named(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = (
            await maria.put(
                f"/api/merge-schedule/{REPO}/flaky",
                json={
                    "diagnose": "patterns",
                    "max_reruns": 3,
                    "backoff_seconds": 60,
                    "rerun_unknown": 1,
                    "root_cause": False,
                },
            )
        ).json()
        assert out["applied"], out
        # Drafted before the draft named reruns, labels, or merge methods.
        await h.store.set_state(
            f"ci-rules:{REPO}",
            {
                "draft": {"how": "CI starts on run-ci.", "gate_label": "run-ci"},
                "dropped": [],
                "by": "maria",
                "at": T0.isoformat(),
            },
        )
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        drafted = view["draft"]["draft"]
        assert drafted["blocking_roles"] == view["rules"]["blocking_roles"]
        assert drafted["max_reruns"] == 3 and drafted["diagnose"] == "patterns"
        saved = (await maria.post(f"/api/merge-schedule/{REPO}/draft/apply")).json()
        assert saved["applied"], saved
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    assert profile.ci.start.how == "CI starts on run-ci."
    assert profile.ci.rerun.max_per_sha == 3, "what the draft did not name stays"
    assert profile.ci.diagnose == "patterns" and profile.ci.root_cause is False


async def test_a_pr_whose_ci_ran_again_and_passed_leaves_the_failure_lane(
    harness: Harness,
) -> None:
    h = harness
    h.runner.on(
        "ci_diagnose",
        lambda t: {"category": "pr_caused", "confidence": 0.9, "explanation": "breaks"},
    )
    h.runner.on("ci_root_cause", lambda t: {"causes": []})
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "failure")))
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "failed"
    # Someone reran it on GitHub, at the same commit, and it passed.
    pr = await h.forge.get_pr(REPO, 42)
    passed = _job(421, "unit", "success", 4200, workflow="CI", attempt=2)
    h.forge.put_pr(pr.model_copy(update={"checks": [passed]}))
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]


async def test_a_merge_refused_for_a_scope_waits_blocked_without_the_turn(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    h.forge.put_pr(_ready(43, "c" * 40, _ci(43, "success")))
    merge = h.forge.merge
    merge_and_move = None

    async def refuse_42(repo, number, *args, **kwargs):  # type: ignore[no-untyped-def]
        if number == 42:
            raise RuntimeError(
                "GitHub API error 403: refusing to allow an OAuth App to create or "
                "update workflow `.github/workflows/ci.yml` without `workflow` scope"
            )
        return await merge(repo, number, *args, **kwargs)

    h.forge.merge = refuse_42  # type: ignore[method-assign]
    await _schedule(h, 42)
    await _schedule(h, 43)
    await h.settle()
    state = await _phase(h, 42)
    assert state.phase == "blocked" and "workflow scope" in state.detail
    assert state.blocked_on == "workflow_scope"
    # Not in the failure lane, and the next PR took the turn and merged.
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43]
    # Checks finishing do not lift a missing scope: it waits.
    h.forge.merge = merge  # type: ignore[method-assign]
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert (await _phase(h, 42)).phase == "blocked"
    # The service starts again, with a sign-in that has the scope: it merges.
    h.clock.advance(minutes=5)
    h.service.info.started_at = h.clock.now()
    await h.emit(Event(kind=EventKind.PR_RECONCILED, repo=REPO, number=42))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [43, 42]
    del merge_and_move


class FakeGrant:
    """A scope request the test approves."""

    code = "ABCD-1234"
    url = "https://github.com/login/device"

    def __init__(self) -> None:
        import asyncio

        self.approved = asyncio.Event()
        self._state = "pending"

    async def start(self, wait: float = 30) -> dict[str, str]:
        return {"code": self.code, "url": self.url}

    async def wait(self) -> bool:
        await self.approved.wait()
        self._state = "done"
        return True

    def state(self) -> str:
        return self._state

    def cancel(self) -> None:
        pass


async def test_a_missing_scope_is_asked_for_and_the_merge_goes_once_granted(
    harness: Harness,
) -> None:
    import asyncio

    h = harness
    grants: list[FakeGrant] = []
    forgotten: list[str | None] = []

    def grant_scopes(repo, scopes):  # type: ignore[no-untyped-def]
        assert scopes == ["workflow"]
        grants.append(FakeGrant())
        return grants[-1]

    h.service.a.grant_scopes = grant_scopes
    h.service.a.forget_token = forgotten.append
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    merge = h.forge.merge
    signed = {"workflow": False}

    async def maybe_refuse(repo, number, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not signed["workflow"]:
            raise RuntimeError(
                "GitHub API error 403: refusing to allow an OAuth App to create or "
                "update workflow `.github/workflows/ci.yml` without `workflow` scope"
            )
        return await merge(repo, number, *args, **kwargs)

    h.forge.merge = maybe_refuse  # type: ignore[method-assign]
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "blocked"
    [grant] = grants
    asked = h.service.scope_request()
    assert asked and asked["code"] == "ABCD-1234" and asked["state"] == "pending"
    # The owner approves on GitHub; the sign-in now has the scope.
    signed["workflow"] = True
    h.clock.advance(minutes=1)
    grant.approved.set()
    for _ in range(50):
        if h.service.scope_request()["state"] == "done":
            break
        await asyncio.sleep(0.01)
    await h.settle()
    assert forgotten == [REPO]
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]


async def test_a_person_diagnoses_or_fixes_only_the_failures_they_chose(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    looked: list[str] = []

    def first_look(task):  # type: ignore[no-untyped-def]
        looked.append(task.prompt)
        return {"category": "pr_caused", "confidence": 0.8, "explanation": "it breaks"}

    h.runner.on("ci_diagnose", first_look)
    h.runner.on("ci_root_cause", lambda t: {"causes": []})
    checks = [
        _job(420, "unit", "failure", 4200, workflow="CI"),
        _job(421, "lint", "failure", 4200, workflow="CI"),
    ]
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.job_logs[421] = "E501 line too long\n"
    h.forge.put_pr(_ready(42, "a" * 40, checks))
    await h.poll()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        gone = await maria.post(
            f"/api/prs/{REPO}/42/ci-diagnose", json={"jobs": ["docs"]}
        )
        assert gone.status_code == 409 and "docs" in gone.text
        out = await maria.post(
            f"/api/prs/{REPO}/42/ci-diagnose", json={"jobs": ["unit"]}
        )
        assert out.status_code == 200, out.text
        await h.settle()
    seen = {o.check_name for o in await h.store.list_ci_observations(REPO)}
    assert seen == {"unit"}, "lint was not chosen"
    assert len(looked) == 1 and "E501" not in looked[0]


# -- conflicts the schedule resolves, and steps the forge would refuse ------------------

CONFLICTED = "intro\n<<<<<<< HEAD\nours line\n=======\ntheirs line\n>>>>>>> main\n"


def _conflicting(h: Harness, path: str = "README.md") -> Any:
    from auto_maintainer.domain.ports import ConflictSides
    from auto_maintainer.domain.snapshot import Mergeable

    pr = _ready(42, "a" * 40, _ci(42, "success"), mergeable=Mergeable.CONFLICTING)
    h.forge.put_pr(pr)
    h.service.a.workspace.merge_conflicts = {path: CONFLICTED}  # type: ignore[attr-defined]
    h.service.a.workspace.merge_sides = {  # type: ignore[attr-defined]
        path: ConflictSides(
            base="intro\n", ours="intro\nours line\n", theirs="intro\ntheirs line\n"
        )
    }
    return pr


def _resolving(text: str) -> Any:
    from auto_maintainer.domain.agents import AgentResult

    def resolve(task: Any) -> AgentResult:
        for name in ("README.md", "src/core.py"):
            target = Path(task.workdir) / name
            if target.exists():
                target.write_text(text)
        return AgentResult(runtime="scripted", ok=True, text="Kept both lines.")

    return resolve


async def test_a_minor_conflict_is_resolved_on_its_turn_and_pushed_as_the_scheduler(
    harness: Harness,
) -> None:
    from auto_maintainer.domain.snapshot import Mergeable

    h = harness
    pr = _conflicting(h)
    h.runner.on("fix", _resolving("intro\nours line\ntheirs line\n"))
    await _schedule(h, 42)
    await h.settle()
    [fix] = await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_FIX])
    assert fix.status == ActionStatus.EXECUTED
    assert fix.approver == "maria" and fix.post_as == "maria", (
        "pushed with the scheduler's own sign-in, never as the service"
    )
    publisher = await h.service.a.publisher_for_user("maria", None)  # type: ignore[misc]
    [pushed] = publisher.published
    assert pushed["message"].startswith("Merge main into #42")
    assert pushed["resolved"] == {"README.md": "intro\nours line\ntheirs line\n"}
    assert (await _phase(h, 42)).phase == "resolving"

    # The push lands: the scheduler's merge commit keeps the schedule, and it merges.
    merged_in = Commit(sha="e" * 40, message=pushed["message"], author_login="maria")
    h.forge.put_pr(
        pr.model_copy(
            update={
                "head_sha": "e" * 40,
                "mergeable": Mergeable.MERGEABLE,
                "commits": [*pr.commits, merged_in],
                "checks": _ci(42, "success", run=4201),
            }
        )
    )
    await h.emit(
        Event(kind=EventKind.PR_SYNCHRONIZED, repo=REPO, number=42, sha="e" * 40)
    )
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]


async def test_a_conflict_in_code_goes_to_a_person_without_an_agent(
    harness: Harness,
) -> None:
    h = harness
    _conflicting(h, path="src/core.py")
    await _schedule(h, 42)
    await h.settle()
    failed = await _phase(h, 42)
    assert failed.phase == "failed"
    assert "src/core.py is not of a kind the schedule resolves" in failed.detail
    assert not [t for t in h.runner.tasks if str(t.kind) == "fix"]
    assert not await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_FIX])


async def test_a_resolution_that_writes_something_new_is_not_pushed(
    harness: Harness,
) -> None:
    h = harness
    _conflicting(h)
    h.runner.on("fix", _resolving("intro\na brand new sentence\n"))
    await _schedule(h, 42)
    await h.settle()
    failed = await _phase(h, 42)
    assert failed.phase == "failed" and "neither side has" in failed.detail
    assert not await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_FIX])


async def test_with_resolving_off_a_conflict_goes_to_the_failure_lane(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        layer={
            "merge": {"enabled": True, "schedule": {"resolve": {"enabled": False}}},
        },
    )
    try:
        await h.service.a.user_tokens.set("maria", UserToken(token="m"))
        h.forge.set_permission(REPO, "bob", Permission.WRITE)
        _conflicting(h)
        await _schedule(h, 42)
        await h.settle()
        failed = await _phase(h, 42)
        assert failed.phase == "failed" and "conflicts with main" in failed.detail
        assert not [t for t in h.runner.tasks if str(t.kind) == "fix"]
    finally:
        await h.close()


async def test_a_fork_that_lets_no_maintainer_push_is_blocked_before_trying(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(
        _ready(
            42,
            "a" * 40,
            _ci(42, "success"),
            behind_base=True,
            head_repo="alice/widget",
            maintainer_can_modify=False,
        )
    )
    await _schedule(h, 42)
    await h.settle()
    blocked = await _phase(h, 42)
    assert blocked.phase == "blocked"
    assert "does not let maintainers push to its branch" in blocked.detail
    assert not h.forge.writes_of("update_branch")


async def test_a_step_the_rate_limit_refused_is_taken_again_after_it_resets(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    h.forge.fail_next["merge"] = RuntimeError(
        "GitHubError: API rate limit exceeded for user"
    )
    await _schedule(h, 42)
    await h.settle()
    state = await _phase(h, 42)
    assert state.phase != "blocked" and state.not_before is not None
    assert "rate limit" in state.detail and not h.forge.writes_of("merge")
    h.clock.advance(minutes=11)
    await h.emit(Event(kind=EventKind.MERGE_QUEUE, repo=REPO, number=42))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42], (
        "taken again as a new action once the limit reset"
    )


async def test_a_merge_github_still_blocks_is_held_not_forced(
    harness: Harness,
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success"), merge_state="BLOCKED"))
    await _schedule(h, 42)
    await h.settle()
    held = await _phase(h, 42)
    assert (
        held.phase == "blocked"
        and "GitHub still reports its merge blocked" in held.detail
    )
    assert not h.forge.writes_of("merge")
    # GitHub's verdict clears: the merge goes on its next look.
    pr = await h.forge.get_pr(REPO, 42)
    h.forge.put_pr(pr.model_copy(update={"merge_state": "CLEAN"}))
    await h.emit(Event(kind=EventKind.MERGE_QUEUE, repo=REPO, number=42))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]


async def test_a_branch_with_a_merge_queue_gets_the_pr_queued(
    harness: Harness,
) -> None:
    h = harness
    info = h.forge.repos[REPO]
    h.forge.add_repo(info.model_copy(update={"merge_queue": True}))
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success"), merge_state="BLOCKED"))
    await _schedule(h, 42)
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("enqueue_merge")] == [42]
    assert not h.forge.writes_of("merge")
    state = await _phase(h, 42)
    assert state.phase == "merging" and state.queued
    assert await h.store.get_arm(REPO, 42) is not None, "the queue merges it later"
    # The queue lets it go without merging: it is blocked, to be queued again.
    h.forge.merge_queue[(REPO, 42)] = False
    await h.emit(Event(kind=EventKind.MERGE_QUEUE, repo=REPO, number=42))
    await h.settle()
    assert (await _phase(h, 42)).phase == "blocked"


async def test_a_required_check_whose_workflow_skips_the_pr_is_named_at_once(
    harness: Harness,
) -> None:
    h = harness
    info = h.forge.repos[REPO]
    h.forge.add_repo(info.model_copy(update={"required_checks": ["build-docs"]}))
    h.forge.put_file(
        REPO,
        ".github/workflows/docs.yml",
        "on:\n  pull_request:\n    paths: ['docs/**']\njobs:\n  build-docs: {}\n",
    )
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success")))
    await _schedule(h, 42)
    await h.settle()
    state = await _phase(h, 42)
    assert "build-docs never runs for this PR" in state.finding
    assert not [t for t in h.runner.tasks if str(t.kind) == "ci_diagnose"], (
        "no agent is asked what the workflow already says"
    )


async def test_the_merge_settings_are_edited_in_place_by_a_maintainer(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        view = (await maria.get(f"/api/merge-schedule/{REPO}/view")).json()
        assert [k["name"] for k in view["merging"]["resolve"]["kinds"]] == [
            "readme",
            "docs",
            "install_targets",
        ]
        bad = await maria.put(
            f"/api/merge-schedule/{REPO}/merging", json={"subject": "{nope}"}
        )
        assert bad.status_code == 422
        saved = await maria.put(
            f"/api/merge-schedule/{REPO}/merging",
            json={
                "method": "squash",
                "signoff_from_authors": True,
                "required_workflows": ["CI Test"],
                "arming_who": "maintain",
                "resolve": {
                    "enabled": True,
                    "union_only": True,
                    "kinds": [
                        {
                            "name": "install_targets",
                            "paths": ["requirements/install.sh"],
                            "description": "the case of install targets",
                        }
                    ],
                },
            },
        )
        assert saved.status_code == 200 and saved.json()["applied"], saved.text
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    assert profile.merge.message.signoff_from_authors
    assert profile.ci.required_workflows == ["CI Test"]
    assert profile.merge.arming.who == "maintain"
    assert [k.paths for k in profile.merge.schedule.resolve.kinds] == [
        ["requirements/install.sh"]
    ]
    async with _as(app, "bob") as bob:
        proposed = await bob.put(
            f"/api/merge-schedule/{REPO}/merging", json={"enabled": False}
        )
        assert proposed.status_code == 200 and not proposed.json()["applied"], (
            "a writer's change goes to the maintainers"
        )


async def test_checking_one_scheduled_pr_has_the_schedule_look_again(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, None)))
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "running"
    pr = await h.forge.get_pr(REPO, 42)
    h.forge.put_pr(pr.model_copy(update={"checks": _ci(42, "success")}))
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "bob") as bob:
        out = await bob.post(f"/api/merge-schedule/{REPO}/42/check")
        assert out.status_code == 200, out.text
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]


async def test_a_flaky_read_the_docs_build_teaches_a_pattern_from_its_build_log(
    harness: Harness,
) -> None:
    from auto_maintainer.domain.records import CiObservation
    from auto_maintainer.playbooks.flake_learn import COMMAND, SUGGESTIONS

    h = harness
    url = "https://app.readthedocs.org/projects/widget/builds/9/"
    h.forge.status_logs[url] = (
        "Read the Docs build 9: failed\n$ git fetch origin pull/42/head  (exit 0)\n"
        "$ git checkout --force abc  (exit 128)\nfatal: unable to read tree (abc)\n"
    )
    obs = CiObservation(
        repo=REPO,
        number=42,
        sha="a" * 40,
        check_id=9,
        check_name="docs/readthedocs.org:widget",
        details_url=url,
        category="unknown",
        verdict="flaky",
        verdict_by="maria",
    )
    await h.store.add_ci_observation(obs)
    prompts: list[str] = []

    def learn(task: Any) -> dict[str, Any]:
        prompts.append(task.prompt)
        return {
            "id": "rtd-unreadable-tree",
            "pattern": r"fatal: unable to read tree",
            "category": "infra",
            "description": "Read the Docs fetched a stale pull ref.",
        }

    h.runner.on("ci_diagnose", learn)
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            payload={"command": COMMAND, "observation": obs.id, "by": "maria"},
        )
    )
    await h.settle()
    assert prompts and "unable to read tree" in prompts[0]
    [suggested] = await h.store.get_state(SUGGESTIONS.format(repo=REPO))
    assert suggested["id"] == "rtd-unreadable-tree"


def test_an_agents_draft_keeps_only_valid_kinds_of_conflict() -> None:
    from auto_maintainer.playbooks.ci_rules import (
        CiRulesDraft,
        ResolveKindDraft,
        checked,
    )

    draft = CiRulesDraft(
        resolve_kinds=[
            ResolveKindDraft(name="install_targets", paths=["requirements/install.sh"]),
            ResolveKindDraft(name="Bad Name!", paths=["x"]),
            ResolveKindDraft(name="nopaths", paths=[]),
            ResolveKindDraft(name="install_targets", paths=["setup.py"]),
        ]
    )
    kept, dropped = checked(draft, "", [])
    assert [k.name for k in kept.resolve_kinds] == ["install_targets"]
    assert len(dropped) == 3


async def test_a_resolution_waits_for_a_scheduler_without_a_sign_in(
    harness: Harness,
) -> None:
    h = harness
    _conflicting(h)
    h.runner.on("fix", _resolving("intro\nours line\ntheirs line\n"))
    await _schedule(h, 42, by="bob")  # bob has no dashboard sign-in to push with
    await h.settle()
    [fix] = await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_FIX])
    assert fix.status == ActionStatus.PROPOSED and fix.action.draft_for == "bob"
    state = await _phase(h, 42)
    assert state.phase == "resolving" and "waits for bob to approve" in state.detail


async def test_an_automatic_schedule_never_pushes_a_resolution_by_itself(
    harness: Harness,
) -> None:
    h = harness
    _conflicting(h)
    h.runner.on("fix", _resolving("intro\nours line\ntheirs line\n"))
    await _schedule(h, 42, auto=True)
    await h.settle()
    [fix] = await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_FIX])
    assert fix.status == ActionStatus.PROPOSED, "maria never saw this PR"


def _rtd(conclusion: str | None) -> CheckRun:
    """Read the Docs' build of the PR: a commit status, with no run to rerun."""
    return CheckRun(
        id=9001,
        name="docs/readthedocs.org:widget",
        status=CheckStatus.COMPLETED if conclusion else CheckStatus.IN_PROGRESS,
        conclusion=CheckConclusion(conclusion) if conclusion else None,
        details_url="https://app.readthedocs.org/projects/widget/builds/9/",
    )


def _flaky(h: Harness) -> None:
    """The first look calls the unit job a flake, and the docs build the runner's."""

    def look(task: Any) -> dict[str, Any]:
        one = {"confidence": 0.9, "explanation": "not the change"}
        if "readthedocs" in task.prompt and "unit" in task.prompt:
            return {
                "jobs": [
                    {**one, "job": "unit", "category": "flaky"},
                    {**one, "job": "docs/readthedocs.org:widget", "category": "infra"},
                ]
            }
        return {**one, "category": "flaky"}

    h.runner.on("ci_diagnose", look)


async def test_a_finished_runs_flake_is_rerun_while_read_the_docs_still_builds(
    harness: Harness,
) -> None:
    h = harness
    _flaky(h)
    h.forge.job_logs[420] = "NCCL timeout on the runner\n"
    h.forge.put_pr(_ready(42, "a" * 40, [*_ci(42, "failure"), _rtd(None)]))
    await _schedule(h, 42)
    await h.settle()
    assert h.forge.writes_of("rerun_failed_jobs") == [{"repo": REPO, "run_id": 4200}]
    state = await _phase(h, 42)
    assert state.phase == "rerunning" and "while the rest of CI runs" in state.detail


async def test_a_failed_docs_build_does_not_hold_back_the_others_reruns(
    harness: Harness,
) -> None:
    h = harness
    # As on RLinf, the branch rules require the docs build.
    info = h.forge.repos[REPO]
    h.forge.add_repo(
        info.model_copy(update={"required_checks": ["docs/readthedocs.org:widget"]})
    )
    _flaky(h)
    h.runner.on("ci_root_cause", lambda t: {"causes": []})
    h.forge.job_logs[420] = "NCCL timeout on the runner\n"
    pr = _ready(42, "a" * 40, [*_ci(42, "failure"), _rtd("failure")])
    h.forge.put_pr(pr)
    await _schedule(h, 42)
    await h.settle()
    assert h.forge.writes_of("rerun_failed_jobs") == [{"repo": REPO, "run_id": 4200}]
    state = await _phase(h, 42)
    assert state.phase == "rerunning"
    assert (
        "readthedocs" in state.detail and "judged once the reruns end" in state.detail
    )
    # The rerun passes; the docs build still fails: now it goes to the failure lane.
    h.clock.advance(minutes=20)
    h.forge.put_pr(
        pr.model_copy(
            update={
                "checks": [
                    _job(421, "unit", "success", 4200, workflow="CI", attempt=2),
                    _rtd("failure"),
                ]
            }
        )
    )
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    failed = await _phase(h, 42)
    assert failed.phase == "failed" and "readthedocs" in failed.detail
    assert len(h.forge.writes_of("rerun_failed_jobs")) == 1


async def test_a_real_failure_of_a_finished_run_fails_the_pr_while_others_run(
    harness: Harness,
) -> None:
    h = harness
    h.runner.on(
        "ci_diagnose",
        lambda t: {
            "category": "pr_caused",
            "confidence": 0.9,
            "explanation": "it breaks",
        },
    )
    h.runner.on(
        "ci_root_cause",
        lambda t: {
            "causes": [
                {
                    "jobs": ["unit"],
                    "failing": "tests/test_m.py::test_x",
                    "origin": "change",
                    "cause": "The new branch returns None.",
                    "confidence": 0.8,
                }
            ]
        },
    )
    h.forge.job_logs[420] = "FAILED tests/test_m.py::test_x\n"
    h.forge.put_pr(_ready(42, "a" * 40, [*_ci(42, "failure"), _rtd(None)]))
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "failed"
    # Read the Docs still building does not bring it back out of the failure lane.
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert (await _phase(h, 42)).phase == "failed"
    assert not h.forge.writes_of("rerun_failed_jobs")


async def test_a_base_merge_refused_for_the_schedulers_scope_waits_for_a_new_sign_in(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success"), behind_base=True))
    update = h.forge.update_branch
    h.forge.fail_next["update_branch"] = RuntimeError(
        "GitHubError: GitHub API error 403: the base branch's new commits change "
        "workflow files, and maria's dashboard sign-in may not change workflow files "
        "(it lacks the workflow scope)"
    )
    await _schedule(h, 42)
    await h.settle()
    state = await _phase(h, 42)
    assert state.phase == "blocked" and state.blocked_on == "person_scope"
    assert "maria's dashboard sign-in lacks the workflow scope" in state.detail
    assert not h.service.scope_request(), "the service's own sign-in is not the one"
    # A change to the PR does not mend a sign-in: no attempt, it stays blocked.
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=42))
    await h.settle()
    assert (await _phase(h, 42)).phase == "blocked"
    assert not h.forge.writes_of("update_branch")
    # maria signs in again and retries: a new attempt goes, as an action of its own.
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _as(app, "maria") as maria:
        out = await maria.post(f"/api/merge-schedule/{REPO}/42/retry")
        assert out.status_code == 200, out.text
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("update_branch")] == [42]
    del update


async def test_waking_a_blocked_base_merge_sends_a_new_update_each_time(
    harness: Harness,
) -> None:
    # RLinf#1844: the woken update reused the refused one's key, so the pipeline
    # dropped it as a duplicate and the PR sat in "merging main in".
    h = harness
    h.forge.put_pr(_ready(42, "a" * 40, _ci(42, "success"), behind_base=True))
    refusal = (
        "GitHub API error 403: refusing to allow an OAuth App to create or update "
        "workflow `.github/workflows/ci-tests.yml` without `workflow` scope"
    )
    h.forge.fail_next["update_branch"] = RuntimeError(refusal)
    await _schedule(h, 42)
    await h.settle()
    assert (await _phase(h, 42)).phase == "blocked"

    async def wake() -> None:
        # A new sign-in wakes what a missing scope held back.
        h.clock.advance(minutes=5)
        h.service.info.started_at = h.clock.now()
        await h.service._wake_blocked(REPO)
        await h.settle()

    # Woken, it tries again, and GitHub refuses that attempt too.
    h.forge.fail_next["update_branch"] = RuntimeError(refusal)
    await wake()
    assert (await _phase(h, 42)).phase == "blocked"
    updates = await h.store.list_actions(repo=REPO, kinds=[ActionKind.PR_UPDATE_BRANCH])
    assert len({u.action.idempotency_key for u in updates}) == 2
    # Woken again, the third attempt goes through.
    await wake()
    assert [w["number"] for w in h.forge.writes_of("update_branch")] == [42]
    assert (await _phase(h, 42)).phase != "blocked"
