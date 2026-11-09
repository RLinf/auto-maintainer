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
from auto_maintainer.testing.fakes import make_pr
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
    harness.runner.on("review", lambda t: REVIEW)
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
    assert "enhancement" not in second.prompt.split("Only these exist:")[-1]
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
