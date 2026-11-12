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
