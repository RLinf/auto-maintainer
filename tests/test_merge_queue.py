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

"""The merge queue: armed PRs take turns so a merge does not waste the others' CI,
the PR whose CI costs most merging first when it can merge as it is."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.merge_gate import MergeArm
from auto_maintainer.domain.merge_queue import QueueEntry, merge_order, next_head
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    PrFile,
    PrState,
    Review,
    ReviewEventState,
)
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness

REPO = "acme/widget"
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _e(number: int, outcome: str, minutes: float, armed: int = 0, ci_only=False):
    return QueueEntry(
        number=number,
        outcome=outcome,  # type: ignore[arg-type]
        ci_only=ci_only,
        ci_seconds=minutes * 60,
        armed_at=T0 + timedelta(minutes=armed),
    )


# -- the order -----------------------------------------------------------------------


def test_a_pr_that_can_merge_as_it_is_goes_first_the_costliest_ci_first() -> None:
    docs, code = _e(43, "merge", 1, armed=0), _e(42, "merge", 40, armed=5)
    order = merge_order([docs, code], None)
    assert [e.number for e in order] == [42, 43]
    assert next_head([docs, code], None) == 42


def test_prs_that_must_update_go_cheapest_first_after_those_ready_as_they_are() -> None:
    entries = [
        _e(1, "update_branch", 40),
        _e(2, "update_branch", 2),
        _e(3, "merge", 5),
        _e(4, "wait", 1),
        _e(5, "blocked", 1),
    ]
    assert [e.number for e in merge_order(entries, None)] == [3, 2, 1, 4, 5]


def test_the_turn_stays_while_the_head_waits_for_its_ci_and_moves_when_it_fails() -> (
    None
):
    running = _e(42, "wait", 40, ci_only=True)
    other = _e(43, "merge", 1)
    assert next_head([running, other], 42) == 42
    failed = _e(42, "blocked", 40)
    assert next_head([failed, other], 42) == 43
    waiting_for_approval = _e(42, "wait", 40, ci_only=False)
    assert next_head([waiting_for_approval, other], 42) == 43
    assert next_head([_e(44, "wait", 1)], None) is None


# -- the flow ------------------------------------------------------------------------


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await Harness.create(
        tmp_path,
        layer={
            "merge": {"enabled": True},
            "ci": {"required_workflows": ["CI"]},
            "policy": {
                "pr.merge": {"level": "auto"},
                "pr.update_branch": {"level": "auto"},
                "comment.upsert": {"level": "auto"},
            },
        },
    )
    h.forge.set_permission(REPO, "bob", Permission.WRITE)
    yield h
    await h.close()


def _ready(number: int, path: str, minutes: int, sha: str):
    return make_pr(
        number=number,
        sha=sha,
        title=f"change {number}",
        files=[PrFile(path=path, additions=5)],
        reviews=[
            Review(
                id=number,
                author="bob",
                state=ReviewEventState.APPROVED,
                commit_sha=sha,
            )
        ],
        checks=[
            CheckRun(
                id=number,
                name="unit",
                workflow="CI",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.SUCCESS,
                run_id=number * 10,
                started_at=T0,
                completed_at=T0 + timedelta(minutes=minutes),
            )
        ],
    )


async def _arm(h: Harness, number: int, minutes_later: int) -> None:
    pr = await h.forge.get_pr(REPO, number)
    assert pr is not None
    at = h.clock.now() + timedelta(minutes=minutes_later)
    await h.store.save_arm(
        MergeArm(
            repo=REPO,
            number=number,
            sha=pr.head_sha,
            armed_by="maria",
            armed_at=at,
            armer_permission=Permission.MAINTAIN,
        )
    )
    # published without settling, so both PRs are armed before either job runs
    await h.service.bus.publish(
        Event(
            kind=EventKind.MERGE_ARMED,
            repo=REPO,
            number=number,
            payload={"by": "maria", "armed_at": at.isoformat()},
        )
    )


async def test_armed_prs_take_turns_code_before_docs(harness: Harness) -> None:
    h = harness
    h.forge.put_pr(_ready(43, "docs/batching.md", 1, "d" * 40))
    h.forge.put_pr(_ready(42, "src/loader.py", 40, "c" * 40))
    merge = h.forge.merge

    async def merge_and_move_main(repo, number, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = await merge(repo, number, *args, **kwargs)
        for (r, n), pr in list(h.forge.prs.items()):
            if r == repo and n != number and pr.state == PrState.OPEN:
                h.forge.put_pr(pr.model_copy(update={"behind_base": True}))
        return out

    h.forge.merge = merge_and_move_main  # type: ignore[method-assign]

    # the docs PR was armed first, but the code PR, whose CI costs 40 times more,
    # merges first: merging docs first would make the code PR run its CI again
    await _arm(h, 43, 0)
    await _arm(h, 42, 5)
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42]
    # main moved, so the docs PR merges main, the cheap part
    assert [w["number"] for w in h.forge.writes_of("update_branch")] == [43]
    state = await h.store.get_state(f"merge-queue:{REPO}")
    assert state["head"] == 43

    # its CI passes on the updated branch, and it merges in turn
    docs = await h.forge.get_pr(REPO, 43)
    assert docs is not None
    h.forge.put_pr(docs.model_copy(update={"behind_base": False}))
    await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=43))
    await h.settle()
    assert [w["number"] for w in h.forge.writes_of("merge")] == [42, 43]
    assert (await h.store.get_state(f"merge-queue:{REPO}"))["head"] is None


async def test_a_queued_pr_does_not_update_while_another_has_the_turn(
    harness: Harness,
) -> None:
    h = harness
    behind = _ready(42, "src/loader.py", 40, "c" * 40).model_copy(
        update={"behind_base": True}
    )
    other = _ready(44, "src/cli.py", 20, "e" * 40).model_copy(
        update={"behind_base": True}
    )
    h.forge.put_pr(behind)
    h.forge.put_pr(other)
    await _arm(h, 42, 0)
    await _arm(h, 44, 1)
    await h.settle()
    # both must merge main; the one with the cheaper CI takes the turn, the other waits
    assert [w["number"] for w in h.forge.writes_of("update_branch")] == [44]
    assert not h.forge.writes_of("merge")
    jobs = [j for j in await h.store.list_jobs() if j.playbook == "merge"]
    waiting = [j for j in jobs if j.number == 42]
    assert waiting and waiting[0].wait and "#44 merges first" in waiting[0].wait.reason
