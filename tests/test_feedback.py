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

"""What reviewers turn down: the PR remembers it, and the repository may learn from
its reason (``playbooks.feedback``, ``playbooks.learn``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_maintainer.domain.actions import ActionKind, ProposalState
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.memory import Tier
from auto_maintainer.domain.snapshot import Commit, PrFile, RepoInfo
from auto_maintainer.testing.fakes import make_pr, report
from auto_maintainer.testing.harness import Harness

REPO = "acme/widget"
REASON = "Support for a new simulator is a new feature; enhancement improves one."
REVIEW = {
    "summary": "Adds a simulator.",
    "findings": [],
    "recommendation": "Ready",
    "labels": ["enhancement", "documentation"],
}


@pytest.fixture
async def h(tmp_path: Path):
    harness = await Harness.create(tmp_path, layer={"extends": ["dco"]})
    harness.forge.repos[REPO] = RepoInfo(
        name=REPO, labels=["enhancement", "feature", "documentation"]
    )
    harness.forge.put_pr(
        make_pr(
            number=42,
            sha="a" * 40,
            files=[PrFile(path="src/sim.py")],
            commits=[
                Commit(sha="a" * 40, message="feat: sim\n\nSigned-off-by: A <a@x>")
            ],
        )
    )
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on("review", lambda t: report())
    harness.runner.on("form", lambda t: REVIEW)
    harness.runner.on("learn", lambda t: {"disposition": "PR_SPECIFIC", "why": "w"})
    yield harness
    await harness.close()


def _request(by: str = "maria") -> Event:
    return Event(
        kind=EventKind.DASHBOARD_COMMAND,
        repo=REPO,
        number=42,
        payload={"command": "review", "by": by, "source": "dashboard"},
    )


async def _labels_proposal(h: Harness):
    for p in await h.store.list_proposals(ProposalState.PENDING, REPO):
        record = await h.store.get_action(p.action_id)
        if record.action.kind == ActionKind.LABEL_APPLY:
            return p, record
    return None, None


def _tasks(h: Harness, kind: str) -> list:
    return [t for t in h.runner.tasks if str(t.kind) == kind]


async def test_rejected_labels_stay_out_and_the_reason_reaches_the_next_review(
    h: Harness,
) -> None:
    await h.emit(_request())
    proposal, _ = await _labels_proposal(h)
    assert proposal is not None
    await h.service.pipeline.reject(proposal.id, "maria", REASON)
    await h.settle()

    await h.emit(_request())
    second = _tasks(h, "review")[-1]
    assert REASON in second.prompt and "turned these proposals down" in second.prompt
    form = _tasks(h, "form")[-1]
    offered = form.prompt.split("source='repository.labels'")[1].split("</untrusted")[0]
    assert "feature" in offered and "enhancement" not in offered
    again, _ = await _labels_proposal(h)
    assert again is None, "the agent named them again, and they were dropped"


async def test_a_label_left_out_before_approving_is_not_proposed_again(
    h: Harness,
) -> None:
    await h.emit(_request())
    proposal, record = await _labels_proposal(h)
    edited = {**record.action.payload, "labels": ["documentation"]}
    await h.service.pipeline.approve(proposal.id, "maria", edited)
    await h.settle()
    [added] = h.forge.writes_of("add_labels")
    assert added["labels"] == ["documentation"]
    decided = await h.store.get_proposal(proposal.id)
    assert decided is not None and decided.declined == ["enhancement"]
    assert not _tasks(h, "learn"), "only a rejection with a reason is learned from"

    await h.emit(_request())
    assert (await _labels_proposal(h))[0] is None


async def test_a_reason_that_holds_for_the_repository_becomes_a_lesson(
    h: Harness,
) -> None:
    lesson = (
        "A label `enhancement` improves an existing feature; new support is `feature`."
    )
    h.runner.on(
        "learn",
        lambda t: {
            "disposition": "LESSON",
            "lesson": lesson,
            "why": "it says what a label means here",
            "message": "Saved the lesson.",
        },
    )
    await h.emit(_request())
    proposal, _ = await _labels_proposal(h)
    await h.service.pipeline.reject(proposal.id, "maria", REASON)
    await h.settle()

    [task] = _tasks(h, "learn")
    assert REASON in task.prompt
    changes = [
        c for c in await h.store.list_memory_changes() if c.title.startswith("lesson")
    ]
    [change] = changes
    assert change.tier == Tier.T1 and change.repos == [REPO]
    assert not any(change.approvals.values()), "the agent wrote it; nobody approved it"
    [text] = change.files.values()
    assert text is not None and f"- {lesson}" in text
    [thread] = await h.store.list_threads("maria")
    [message] = await h.store.list_messages(thread.id)
    assert message.text == "Saved the lesson."


async def test_a_reason_about_the_pr_alone_teaches_nothing(h: Harness) -> None:
    await h.emit(_request())
    proposal, _ = await _labels_proposal(h)
    await h.service.pipeline.reject(proposal.id, "maria", "not on this PR")
    await h.settle()
    assert len(_tasks(h, "learn")) == 1
    assert not [
        c for c in await h.store.list_memory_changes() if c.title.startswith("lesson")
    ]


async def test_rejection_learning_deduplicates_against_pending_and_consolidates(
    h: Harness,
) -> None:
    from auto_maintainer.domain.memory import (
        MemoryChange,
        MemoryChangeState,
        Tier,
        repo_dir,
    )

    path = f"{repo_dir(REPO)}/knowledge/review_lessons.md"
    h.memory.files[path] = (
        "# Review lessons\n"
        "- Use `feature` for new simulator support.\n"
        "- New simulator support uses the `feature` label.\n"
    )
    pending_lesson = "Reserve `enhancement` for improvements to existing features."
    await h.store.save_memory_change(
        MemoryChange(
            tier=Tier.T1,
            title="pending rejection lesson",
            files={path: h.memory.files[path] + f"- {pending_lesson}\n"},
            repos=[REPO],
        )
    )

    # First: agent repeats a lesson already waiting in an open memory change -> dropped.
    h.runner.on(
        "learn",
        lambda t: {
            "disposition": "LESSON",
            "lesson": pending_lesson.lower().rstrip("."),
            "why": "label rule",
            "message": "Saved.",
        },
    )
    await h.emit(_request())
    proposal, _ = await _labels_proposal(h)
    assert proposal is not None
    await h.service.pipeline.reject(proposal.id, "maria", REASON)
    await h.settle()

    assert pending_lesson in _tasks(h, "learn")[-1].prompt
    lesson_changes = [
        c
        for c in await h.store.list_memory_changes(MemoryChangeState.OPEN)
        if c.title.startswith("lesson from")
    ]
    assert lesson_changes == [], "duplicate of pending lesson creates no new change"
    assert await h.store.list_threads("maria") == []

    # Second: agent consolidates the two redundant bullets in current review_lessons.md.
    consolidated = (
        "Use `feature` for new simulator support and `enhancement` for improvements."
    )
    h.runner.on(
        "learn",
        lambda t: {
            "disposition": "LESSON",
            "lesson": consolidated,
            "replaces": [
                "Use `feature` for new simulator support.",
                "New simulator support uses the `feature` label.",
            ],
            "why": "consolidates duplicate label rules",
            "message": "Consolidated the lesson.",
        },
    )
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=42,
            payload={"command": "review", "by": "walt", "source": "dashboard"},
        )
    )
    # Trigger a fresh rejection directly on a second review proposal.
    review_prop = next(
        p
        for p in await h.store.list_proposals(ProposalState.PENDING, REPO)
        if p.requested_by == "walt"
    )
    await h.service.pipeline.reject(review_prop.id, "walt", REASON)
    await h.settle()

    changes = [
        c
        for c in await h.store.list_memory_changes(MemoryChangeState.OPEN)
        if c.title.startswith("lesson from")
    ]
    [change] = changes
    text = change.files[path]
    assert text is not None
    assert text == f"# Review lessons\n- {consolidated}\n"


async def test_rejecting_without_a_reason_succeeds_and_skips_learning(
    h: Harness,
) -> None:
    """Rejections allow omitting the reason: the proposal is rejected, learning
    skips without running an agent, and the next prompt omits an empty Why."""
    await h.emit(_request())
    proposal, _ = await _labels_proposal(h)
    assert proposal is not None

    rejected = await h.service.pipeline.reject(proposal.id, "maria", "   ")
    await h.settle()

    assert rejected.state == ProposalState.REJECTED
    assert rejected.reason is None
    record = await h.store.get_action(proposal.action_id)
    assert record is not None and record.error == "rejected by maria"
    assert not _tasks(h, "learn")

    from auto_maintainer.playbooks.feedback import rejections_block, rejections_on

    block = rejections_block(await rejections_on(h.store, REPO, 42))
    assert "maria turned down labels enhancement, documentation at" in block
    assert "Why:" not in block


async def test_rejected_ci_gate_role_is_tracked_and_not_proposed_again(
    h: Harness,
) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.playbooks.feedback import (
        declined_roles,
        rejections_block,
        rejections_on,
    )
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    layer = parse_yaml(h.memory.files[profile_path(REPO)], "settings")
    layer["labels"] = {
        **(layer.get("labels") or {}),
        "roles": {"ci_gate": "run-ci"},
    }
    layer["review"] = {**(layer.get("review") or {}), "suggest_ci_gate": True}
    h.memory.files[profile_path(REPO)] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")

    await h.emit(_request())
    gate_props = [
        p
        for p in await h.store.list_proposals(ProposalState.PENDING, REPO)
        if (await h.store.get_action(p.action_id)).action.kind
        == ActionKind.LABEL_CI_GATE
    ]
    assert len(gate_props) == 1
    gate_prop = gate_props[0]
    reason = "Do not suggest run-ci in reviews."
    await h.service.pipeline.reject(gate_prop.id, "maria", reason)
    await h.settle()

    turned_down = await rejections_on(h.store, REPO, 42)
    assert declined_roles(turned_down) == {"ci_gate"}
    block = rejections_block(turned_down)
    assert "label.ci_gate (ci_gate)" in block and reason in block
    [learn_task] = _tasks(h, "learn")
    assert "'run-ci' (label.ci_gate)" in learn_task.prompt

    await h.emit(_request())
    again = [
        p
        for p in await h.store.list_proposals(ProposalState.PENDING, REPO)
        if (await h.store.get_action(p.action_id)).action.kind
        == ActionKind.LABEL_CI_GATE
    ]
    assert again == []
