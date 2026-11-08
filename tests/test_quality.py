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

"""A PR's quality: the reviewing agent's score, the guides every review reads, and
the scoring of reviews that have none."""

from __future__ import annotations

from pathlib import Path

from auto_maintainer.domain.actions import ActionKind
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.review import review_quality
from auto_maintainer.domain.snapshot import Commit, PrFile
from auto_maintainer.playbooks.prompts import (
    DEFAULT_QUALITY_GUIDE,
    DEFAULT_WRITING_GUIDE,
)
from auto_maintainer.playbooks.quality import assessed_quality
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness

REPO = "acme/widget"
SCORING = "You judge the quality of PR"


def _review(quality: dict | None) -> dict:
    return {
        "summary": "Guards against empty batches.",
        "findings": [],
        "recommendation": "Ready",
        **({"quality": quality} if quality else {}),
    }


async def _reviewed(tmp_path: Path, quality: dict | None, layer=None) -> Harness:
    h = await Harness.create(tmp_path, layer=layer)
    h.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    h.runner.on(
        "evaluate",
        lambda t: {"scores": {}, "summary": "s", "rationale": "r"},
    )
    h.runner.on(
        "review",
        lambda t: (
            {
                "score": 58,
                "reason": "Sound, but the loader change is in the wrong layer.",
            }
            if SCORING in t.prompt
            else _review(quality)
        ),
    )
    sha = "a" * 40
    h.forge.put_pr(
        make_pr(
            number=42,
            sha=sha,
            files=[PrFile(path="src/a.py")],
            commits=[Commit(sha=sha, message="fix: a")],
        )
    )
    await h.poll()
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=42,
            payload={"command": "review", "by": "maria", "source": "dashboard"},
        )
    )
    return h


async def _quality(h: Harness):
    records = await h.store.list_actions(
        repo=REPO, number=42, kinds=[ActionKind.REVIEW_POST]
    )
    return review_quality(records, await assessed_quality(h.store, REPO, 42))


def _scoring(h: Harness) -> list:
    return [t for t in h.runner.tasks if SCORING in t.prompt]


async def test_the_review_scores_quality_by_the_guides_it_is_given(
    tmp_path: Path,
) -> None:
    layer = {"review": {"writing_guide": "Two sentences at most, no code."}}
    h = await _reviewed(tmp_path, {"score": 91, "reason": "Fits the design."}, layer)
    try:
        [task] = [t for t in h.runner.tasks if str(t.kind) == "review"]
        assert "Two sentences at most, no code." in task.prompt
        assert DEFAULT_WRITING_GUIDE not in task.prompt, "the repository's guide wins"
        assert DEFAULT_QUALITY_GUIDE in task.prompt
        q = await _quality(h)
        assert q is not None and (q.score, q.band, q.rescored) == (
            91,
            "excellent",
            False,
        )
        [record] = await h.store.list_actions(
            repo=REPO, number=42, kinds=[ActionKind.REVIEW_POST]
        )
        assert "91" not in record.action.payload["body"], "the score is internal"
    finally:
        await h.close()


async def test_a_review_without_a_score_is_scored_once(tmp_path: Path) -> None:
    h = await _reviewed(tmp_path, None)
    try:
        assert await _quality(h) is None
        tick = Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"})
        await h.emit(tick)
        [task] = _scoring(h)
        assert DEFAULT_QUALITY_GUIDE in task.prompt and "PR: #42" in task.prompt
        q = await _quality(h)
        assert q is not None and (q.score, q.band, q.rescored) == (58, "poor", True)
        assert "wrong layer" in q.reason

        await h.emit(tick.model_copy(update={"id": "evt_again"}))
        assert len(_scoring(h)) == 1, "a review is scored once on its own"

        await h.emit(
            Event(
                kind=EventKind.QUALITY_REQUESTED,
                repo=REPO,
                number=42,
                payload={"by": "maria"},
            )
        )
        assert len(_scoring(h)) == 2, "a maintainer can ask again"
    finally:
        await h.close()
