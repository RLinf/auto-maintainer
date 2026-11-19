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

"""A review in two turns (``playbooks/review_form.py``): the agent follows the review
skill as a person asks for a review and ends with the skill's report; the same agent
then fills in the fields the service posts."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_maintainer.domain.actions import ActionKind
from auto_maintainer.domain.agents import AgentResult, AgentTask
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.snapshot import Commit, PrFile
from auto_maintainer.testing.fakes import make_pr, report
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.jobs import final_output

REPO = "acme/widget"
REPORT = "# Review of #42\n\n## Findings\n\nF1 · major · correctness: the last batch is skipped."
FORM = {
    "summary": "Handles empty batches; skips the last one.",
    "findings": [
        {
            "id": "F1",
            "severity": "major",
            "category": "correctness",
            "title": "The last batch is skipped",
            "body": "The loop stops one batch early.",
            "path": "src/loader.py",
            "line": 1,
        }
    ],
    "recommendation": "Changes requested",
}


@pytest.fixture
async def h(tmp_path: Path):
    harness = await Harness.create(tmp_path)
    harness.service.a.workspace.diff_text = (
        "diff --git a/src/loader.py b/src/loader.py\n--- a/src/loader.py\n"
        "+++ b/src/loader.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    harness.forge.put_pr(
        make_pr(
            number=42,
            files=[PrFile(path="src/loader.py")],
            commits=[Commit(sha="a" * 40, message="fix: loader")],
        )
    )
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate", lambda t: {"scores": {}, "summary": "s", "rationale": "r"}
    )
    harness.runner.on("review", lambda t: report(REPORT))
    yield harness
    await harness.close()


async def _ask(h: Harness) -> None:
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=42,
            payload={"command": "review", "by": "maria", "source": "dashboard"},
        )
    )


def _tasks(h: Harness, kind: str) -> list[AgentTask]:
    return [t for t in h.runner.tasks if str(t.kind) == kind]


async def test_the_reviewer_is_asked_as_a_person_asks_and_fills_in_its_report(
    h: Harness,
) -> None:
    h.runner.on("form", lambda t: FORM)
    await _ask(h)
    [review] = _tasks(h, "review")
    assert review.prompt.startswith("Use the `pr-review` skill (")
    assert "to review PR #42 in acme/widget." in review.prompt.split("\n")[0]
    assert "diff --git" not in review.prompt, "the agent reads the diff itself"
    assert "Fill `quality`" not in review.prompt and "`labels`" not in review.prompt
    assert review.output_schema is None, "the review ends with the skill's report"

    [form] = _tasks(h, "form")
    assert form.resume_session == "session-1", "the agent that wrote it fills it in"
    assert REPORT in form.prompt, "an agent that starts afresh has the report"
    assert "Fill `quality`" in form.prompt, "scored once the review is written"
    [draft] = await h.store.list_actions(
        repo=REPO, number=42, kinds=[ActionKind.REVIEW_POST]
    )
    assert draft.action.payload["result"]["findings"][0]["id"] == "F1"
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    steps = await h.service.progress.steps(job.id)
    assert final_output(steps, "review") == REPORT, "its final output is the report"


async def test_a_form_that_fails_runs_again_without_the_review(h: Harness) -> None:
    answers = iter(
        [
            AgentResult(
                runtime="scripted", ok=False, error="it crashed", error_kind="crash"
            ),
            FORM,
        ]
    )
    h.runner.on("form", lambda t: next(answers))
    await _ask(h)
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    assert job.status == "queued", "it runs again later"
    h.clock.advance(minutes=5)
    await h.settle()
    assert len(_tasks(h, "review")) == 1, "the report it wrote is kept"
    first, again = _tasks(h, "form")
    assert again.resume_session == first.resume_session == "session-1"
    assert await h.store.list_actions(
        repo=REPO, number=42, kinds=[ActionKind.REVIEW_POST]
    )
