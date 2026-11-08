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
from auto_maintainer.domain.review import QualityVerdict, review_quality
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    PrFile,
)
from auto_maintainer.playbooks.prompts import (
    DEFAULT_QUALITY_GUIDE,
    DEFAULT_WRITING_GUIDE,
)
from auto_maintainer.playbooks.quality import (
    QUICK_DIFF_LIMIT,
    assessed_quality,
    condensed_diff,
    quick_facts,
    quick_quality,
)
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
        assert "en (English), zh (Simplified Chinese)" in task.prompt
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


QUICK = "You give PR"


async def test_every_open_pr_gets_a_quick_score_until_its_review_scores_it(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    h.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    h.runner.on(
        "evaluate",
        lambda t: (
            {
                "score": 72,
                "reasons": {"en": "Fits the loader's design.", "zh": "符合设计。"},
                "checks": {"requirements": True, "tests": False, "docs": None},
            }
            if t.prompt.startswith(QUICK)
            else {"scores": {}, "summary": "s", "rationale": "r"}
        ),
    )
    h.runner.on(
        "review",
        lambda t: _review({"score": 61, "reason": "The full read found more."}),
    )
    for number, sha in ((42, "a" * 40), (43, "b" * 40)):
        h.forge.put_pr(
            make_pr(
                number=number,
                sha=sha,
                files=[PrFile(path="src/a.py")],
                commits=[Commit(sha=sha, message="fix: a")],
            )
        )
    try:
        await h.poll()
        tick = Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"})
        await h.emit(tick)
        quick = {n: await quick_quality(h.store, REPO, n) for n in (42, 43)}
        assert {n: q.score for n, q in quick.items() if q} == {42: 72, 43: 72}
        assert quick[42] is not None and quick[42].band == "fair"
        asked = [t for t in h.runner.tasks if t.prompt.startswith(QUICK)]
        assert len(asked) == 2 and "PR: #4" in asked[0].prompt
        assert "First fill its `checks`" in asked[0].prompt
        assert "Facts the service collected" in asked[0].prompt
        assert quick[42] is not None and quick[42].checks == {
            "requirements": True,
            "tests": False,
            "docs": None,
        }

        await h.emit(tick.model_copy(update={"id": "evt_again"}))
        assert len([t for t in h.runner.tasks if t.prompt.startswith(QUICK)]) == 2

        await h.emit(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo=REPO,
                number=42,
                payload={"command": "review", "by": "maria", "source": "dashboard"},
            )
        )
        q = await _quality(h)
        assert q is not None and q.score == 61, "the review's score is the accurate one"

        pr = await h.forge.get_pr(REPO, 43)
        h.forge.put_pr(pr.model_copy(update={"head_sha": "c" * 40}))
        await h.poll()
        h.clock.advance(hours=7)
        await h.emit(tick.model_copy(update={"id": "evt_later"}))
        rescored = await quick_quality(h.store, REPO, 43)
        assert rescored is not None and rescored.sha == "c" * 40
        last = [t for t in h.runner.tasks if t.prompt.startswith(QUICK)][-1]
        assert "#42: 61 (fair)" in last.prompt, "a reviewed PR calibrates quick scores"
        assert not [
            t
            for t in h.runner.tasks
            if t.prompt.startswith(QUICK) and "PR: #42" in t.prompt
        ][1:], "a reviewed PR is not scored quickly again"
    finally:
        await h.close()


async def test_quick_scores_can_be_turned_off(tmp_path: Path) -> None:
    h = await _reviewed(tmp_path, None, {"review": {"quick_quality": False}})
    try:
        h.forge.put_pr(make_pr(number=43, sha="b" * 40, files=[PrFile(path="a")]))
        await h.poll()
        await h.emit(
            Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"})
        )
        assert await quick_quality(h.store, REPO, 43) is None
    finally:
        await h.close()


def test_the_reason_comes_in_each_dashboard_language() -> None:
    verdict = QualityVerdict.model_validate(
        {"score": 70, "reasons": {"en": " Sound design. ", "zh": "设计合理。", "x": 3}}
    )
    assert verdict.reasons == {"en": "Sound design.", "zh": "设计合理。"}
    assert verdict.reason == "Sound design.", "the first reason stands in for others"
    assert QualityVerdict.model_validate({"score": 70, "reasons": "no"}).reasons == {}


def test_quick_facts_name_the_basics_the_service_checked() -> None:
    pr = make_pr(
        number=7,
        sha="d" * 40,
        files=[PrFile(path="src/a.py", additions=30), PrFile(path="tests/test_a.py")],
        checks=[
            CheckRun(
                id=1,
                name="unit",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
            )
        ],
    )
    hygiene = {
        "sha": "d" * 40,
        "results": [
            {"id": "title", "passed": False, "summary": "PR title needs changes"},
            {"id": "owners", "passed": True, "summary": "every file has an owner"},
        ],
    }
    facts = quick_facts(pr, hygiene)
    assert "2 files, +30/-0" in facts and "look like tests: 1" in facts
    assert "failing: unit" in facts and "failed: PR title needs changes" in facts
    stale = quick_facts(pr, {**hygiene, "sha": "e" * 40})
    assert "hygiene" not in stale, "results for another head are left out"
    verdict = QualityVerdict.model_validate(
        {"score": 60, "checks": {"tests": False, "docs": None, "style": "yes"}}
    )
    assert verdict.checks == {"tests": False, "docs": None}


def test_a_quick_score_reads_a_condensed_diff() -> None:
    def block(path: str, size: int) -> str:
        return f"diff --git a/{path} b/{path}\n+" + "x" * size + "\n"

    diff = (
        block("tests/test_a.py", 100)
        + block("uv.lock", 50_000)
        + block("src/a.py", 20_000)
        + "".join(block(f"src/m{i}.py", 5_000) for i in range(8))
    )
    out = condensed_diff(diff)
    assert len(out) < QUICK_DIFF_LIMIT + 1_000
    assert out.index("src/a.py") < out.index("tests/test_a.py") or (
        "tests/test_a.py" in out.split("Left out of this diff:")[1]
    ), "source before tests"
    assert "uv.lock" in out.split("Left out of this diff:")[1]
    assert "the rest of this file's diff is left out" in out


async def test_the_review_standard_is_drawn_into_a_checklist_once(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    checklists: list[str] = []

    def evaluate(t):
        if "into a checklist for a quick check" in t.prompt:
            checklists.append(t.prompt)
            return {
                "items": [
                    {"area": "docs", "check": "New features get a page in the docs."},
                    {"area": "style", "check": "Public functions have type hints."},
                ]
            }
        if t.prompt.startswith(QUICK):
            return {"score": 70, "reasons": {"en": "a", "zh": "b"}, "checks": {}}
        return {"scores": {}, "summary": "s", "rationale": "r"}

    h.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    h.runner.on("evaluate", evaluate)
    for number in (42, 43):
        h.forge.put_pr(make_pr(number=number, sha=f"{number:040x}"))
    try:
        await h.poll()
        await h.emit(
            Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "review_scan"})
        )
        quick = [t for t in h.runner.tasks if t.prompt.startswith(QUICK)]
        assert len(quick) == 2 and len(checklists) == 1, "drawn once, then kept"
        assert "- [style] Public functions have type hints." in quick[1].prompt
        assert all(t.budget == "quick" and t.run_code == "none" for t in quick), (
            "quick and reading only"
        )
    finally:
        await h.close()
