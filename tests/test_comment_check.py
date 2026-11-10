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

"""Comment checks: an agent reads only each open comment and the code around it,
the results count toward review progress, and a person resolves a comment as
themselves."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import AgentTask
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.domain.snapshot import ReviewThread, ThreadComment
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
DIFF = (
    "diff --git a/src/loader.py b/src/loader.py\n--- a/src/loader.py\n"
    "+++ b/src/loader.py\n@@ -1,3 +1,4 @@\n def load(batch):\n+    if not batch: return []\n"
    "diff --git a/README.md b/README.md\n+docs\n"
)


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    pr = await h.forge.get_pr(REPO, 42)
    threads = [
        ReviewThread(
            id="T_open",
            path="src/loader.py",
            line=2,
            comments=[
                ThreadComment(
                    id=501,
                    author="bob",
                    body="Return early on an empty batch.\nIt crashes today.",
                    commit_sha="0" * 40,
                )
            ],
        ),
        ReviewThread(
            id="T_done",
            path="README.md",
            is_resolved=True,
            comments=[ThreadComment(id=502, author="bob", body="Typo")],
        ),
        ReviewThread(
            id="T_self",
            path="src/loader.py",
            comments=[ThreadComment(id=503, author=pr.author, body="Note to self")],
        ),
    ]
    pr = pr.model_copy(update={"threads": threads})
    h.forge.put_pr(pr)
    await h.store.save_snapshot(pr)
    h.service.a.workspace.diff_text = DIFF
    checkout = (
        h.service.a.workspace.root / f"{REPO.replace('/', '__')}-42-{pr.head_sha[:7]}"
    )
    (checkout / "src").mkdir(parents=True)
    (checkout / "src/loader.py").write_text(
        "def load(batch):\n    if not batch: return []\n"
    )
    asked: list[AgentTask] = []

    def answer(task: AgentTask) -> dict[str, Any]:
        asked.append(task)
        return {
            "items": [
                {
                    "id": "c1",
                    "summary": "Return early when the batch is empty",
                    "status": "addressed",
                    "reason": "load() now returns [] for an empty batch",
                    "fixed_in": pr.head_sha[:7],
                }
            ]
        }

    h.runner.on("comment_check", answer)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app, asked
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_check_reads_only_each_comment_and_its_code(demo) -> None:
    h, app, asked = demo
    async with _as(app, "maria") as maria:
        started = await maria.post(f"/api/prs/{REPO}/42/comment-check", json={})
        assert started.status_code == 200
        await h.settle()
        [task] = asked
        assert task.budget == "quick"
        assert "Return early on an empty batch." in task.prompt
        assert "+    if not batch: return []" in task.prompt, "its file's changes"
        assert "README.md" not in task.prompt, "only the commented file's changes"
        assert "    2      if not batch" in task.prompt, "the code around its line"
        assert "Note to self" not in task.prompt and "Typo" not in task.prompt

        progress = (await maria.get(f"/api/prs/{REPO}/42/progress")).json()
        [bob] = [r for r in progress["reviewers"] if r["login"] == "bob"]
        assert (bob["threads"], bob["resolved"], bob["fixed"], bob["addressed"]) == (
            2,
            1,
            1,
            1.0,
        )
        item = next(c for c in bob["comments"] if c["thread_id"] == "T_open")
        assert item["done"] and item["check"]["summary"] == (
            "Return early when the batch is empty"
        )
        assert item["excerpt"] == "Return early on an empty batch."
        assert item["url"].endswith("#discussion_r501")
        assert progress["fixed"] == 1 and progress["addressed"] == 1.0

        await maria.post(f"/api/prs/{REPO}/42/comment-check", json={})
        await h.settle()
        assert len(asked) == 1, "nothing changed since its check at this head"
        await maria.post(
            f"/api/prs/{REPO}/42/comment-check", json={"thread_ids": ["T_open"]}
        )
        await h.settle()
        assert len(asked) == 2, "a named comment is checked again"


async def test_the_periodic_check_skips_what_did_not_change(demo) -> None:
    h, app, asked = demo
    tick = Event(kind=EventKind.SCHEDULE_TICK, payload={"name": "comment_check"})
    await h.emit(tick)
    assert len(asked) == 1
    await h.emit(tick.model_copy(update={"id": "evt_soon"}))
    assert len(asked) == 1, "not due yet, and nothing to check"
    h.clock.advance(hours=5)
    await h.emit(tick.model_copy(update={"id": "evt_later"}))
    assert len(asked) == 1, "checked at this head already"

    pr = await h.forge.get_pr(REPO, 42)
    moved = pr.model_copy(update={"head_sha": "c" * 40})
    h.forge.put_pr(moved)
    await h.store.save_snapshot(moved)
    h.service.a.workspace.diff_text = "diff --git a/README.md b/README.md\n+docs\n"
    h.clock.advance(hours=5)
    await h.emit(tick.model_copy(update={"id": "evt_moved"}))
    assert len(asked) == 1, "the commented file did not change since its check"


async def test_a_comment_is_resolved_by_the_person_who_presses_resolve(demo) -> None:
    h, app, _ = demo
    url = f"/api/prs/{REPO}/42/threads/T_open/resolve"
    async with _as(app, "walt") as walt:
        assert (await walt.post(url)).status_code == 403, "not theirs to resolve"
    async with _as(app, "maria") as maria:
        unsigned = await maria.post(url)
        assert unsigned.status_code == 403 and "sign in" in unsigned.json()["detail"]
        await h.service.a.user_tokens.set("maria", UserToken(token="t"))
        done = await maria.post(url)
        assert done.status_code == 200
        assert done.json()["status"] in ("queued", "executed"), done.text
    await h.settle()
    [write] = h.forge.writes_of("resolve_review_thread")
    assert write["thread_id"] == "T_open"
    record = next(
        r
        for r in await h.store.list_actions(repo=REPO, number=42)
        if r.action.kind == "thread.resolve"
    )
    assert record.post_as == "maria", "never as the service's account"
