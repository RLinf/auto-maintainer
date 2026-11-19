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

"""A repository's Lessons tab (design 5.11): lessons with who approved them,
edited through the configuration gate, and summarized by an agent into a draft a
maintainer applies."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.lessons import parse, render
from auto_maintainer.domain.memory import MemoryChange, Tier
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
PATH = "repos/acme/widget/knowledge/review_lessons.md"

AS_KEPT = """# Priority lessons
- Weigh how fast a PR can merge:
1. Raise PRs that are nearly done.
2. Keep P0 for a few:
   - P0: blocking fixes only.
- Reviews stay short.
  Only conclusions and suggestions.
"""


def test_a_lessons_file_reads_as_lessons_and_writes_back_the_same() -> None:
    lessons = parse(AS_KEPT, "Lessons")
    assert lessons.heading == "Priority lessons"
    assert [x.splitlines()[0] for x in lessons.items()] == [
        "Weigh how fast a PR can merge:",
        "Reviews stay short.",
    ]
    assert "- P0: blocking fixes only." in lessons.items()[0], (
        "a lesson's own list stays inside it"
    )
    once = render(lessons)
    assert render(parse(once, "Lessons")) == once
    assert parse(once, "Lessons").items() == lessons.items()

    grouped = parse("# Review lessons\n- loose\n\n## Tests\n- keep them lean\n", "x")
    assert [(s.title, s.items) for s in grouped.sections] == [
        ("", ["loose"]),
        ("Tests", ["keep them lean"]),
    ]


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def _learned(h: Any, text: str, title: str, by: str) -> None:
    """A lesson change merged the way the learning playbooks' changes merge."""
    change = MemoryChange(
        tier=Tier.T1,
        title=title,
        files={PATH: text},
        repos=[REPO],
        created_at=h.clock.now(),
    )
    await h.service.store.save_memory_change(change)
    await h.service.submit_memory_change(change)
    _, state = await h.service.approve_memory_change(change.id, by)
    assert state == "merged"
    h.clock.advance(minutes=5)


def _lessons(page: dict[str, Any], kind: str = "review") -> list[dict[str, Any]]:
    [found] = [f for f in page["files"] if f["kind"] == kind]
    return [item for s in found["sections"] for item in s["items"]]


async def test_each_lesson_shows_who_approved_it(demo) -> None:
    h, app = demo
    await _learned(
        h, "# Review lessons\n- Keep reviews short.\n", "lesson from maria", "maria"
    )
    await _learned(
        h,
        "# Review lessons\n- Keep reviews short.\n- Never ask for the run-ci label.\n",
        "lesson several reviewers hold",
        "maria",
    )
    async with _as(app, "walt") as walt:
        page = (await walt.get(f"/api/repos/{REPO}/lessons")).json()
    assert [
        (x["text"], x["from"]["title"], x["from"]["approved_by"])
        for x in _lessons(page)
    ] == [
        ("Keep reviews short.", "lesson from maria", ["maria"]),
        ("Never ask for the run-ci label.", "lesson several reviewers hold", ["maria"]),
    ]
    assert page["can_configure"] is False


async def test_a_maintainers_edit_applies_and_anyone_elses_waits(demo) -> None:
    h, app = demo
    await _learned(
        h, "# Review lessons\n- Keep reviews short.\n- Be brief.\n", "lesson", "maria"
    )
    url = f"/api/repos/{REPO}/lessons/review"
    async with _as(app, "walt") as walt:
        page = (await walt.get(f"/api/repos/{REPO}/lessons")).json()
        [review] = [f for f in page["files"] if f["kind"] == "review"]
        base = review["text"]
        out = await walt.put(
            url,
            json={
                "base": base,
                "sections": [{"title": "", "items": ["Keep reviews short."]}],
            },
        )
        assert out.status_code == 200, out.text
        assert out.json()["applied"] is False, (
            "a reviewer's edit waits for a maintainer"
        )
        page = (await walt.get(f"/api/repos/{REPO}/lessons")).json()
        [review] = [f for f in page["files"] if f["kind"] == "review"]
        assert len(review["pending"]) == 1 and len(_lessons(page)) == 2
    async with _as(app, "maria") as maria:
        same = await maria.put(
            url,
            json={
                "base": base,
                "sections": [
                    {"title": "", "items": ["Keep reviews short.", "Be brief."]}
                ],
            },
        )
        assert same.status_code == 422, "nothing changed"
        out = await maria.put(
            url,
            json={
                "base": base,
                "sections": [
                    {"title": "Writing", "items": ["Keep reviews short and brief."]}
                ],
            },
        )
        assert out.json()["applied"] is True
        stale = await maria.put(
            url, json={"base": base, "sections": [{"title": "", "items": ["x"]}]}
        )
        assert stale.status_code == 409, "an edit of what changed since is refused"
        page = (await maria.get(f"/api/repos/{REPO}/lessons")).json()
    assert _lessons(page)[0]["text"] == "Keep reviews short and brief."
    assert _lessons(page)[0]["from"]["approved_by"] == ["maria"]
    assert await h.service.a.memory.read_text(PATH) == (
        "# Review lessons\n\n## Writing\n- Keep reviews short and brief.\n"
    )


async def test_an_agent_summarizes_into_a_draft_a_maintainer_applies(demo) -> None:
    h, app = demo
    await _learned(
        h,
        "# Review lessons\n- Keep reviews short.\n- Reviews must be brief.\n"
        "- Do not add the run-ci label.\n",
        "lessons",
        "maria",
    )
    h.runner.on(
        "learn",
        lambda t: {
            "groups": [
                {"title": "Writing", "lessons": ["Keep reviews short."]},
                {"title": "CI", "lessons": ["Do not add the run-ci label."]},
            ],
            "dropped": [
                {"lesson": "Reviews must be brief.", "why": "Same as the first."}
            ],
            "summary": "Merged two lessons about length.",
        },
    )
    async with _as(app, "walt") as walt:
        asked = await walt.post(f"/api/repos/{REPO}/lessons/review/summarize", json={})
        assert asked.status_code == 200, asked.text
        await h.settle()
        live = await walt.get(f"/api/jobs/{asked.json()['event']}")
        assert live.status_code == 200, "its live view opens by the request's id"
        [task] = [t for t in h.runner.tasks if t.kind == "learn"]
        assert task.requested_by == "walt" and "Reviews must be brief." in task.prompt
        page = (await walt.get(f"/api/repos/{REPO}/lessons")).json()
    [review] = [f for f in page["files"] if f["kind"] == "review"]
    [draft] = review["pending"]
    assert draft["summary"] and "Same as the first." in draft["description"]
    assert [s["title"] for s in draft["sections"]] == ["Writing", "CI"]
    assert len(_lessons(page)) == 3, "nothing applies before a maintainer does"
    async with _as(app, "maria") as maria:
        applied = await maria.post(f"/api/memory/changes/{draft['id']}/approve")
        assert applied.json()["state"] == "merged"
        page = (await maria.get(f"/api/repos/{REPO}/lessons")).json()
    assert [x["text"] for x in _lessons(page)] == [
        "Keep reviews short.",
        "Do not add the run-ci label.",
    ]
