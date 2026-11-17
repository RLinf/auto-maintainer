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

"""What keeps the dashboard light on a busy host: the PR list and the agents' work
shared by the tabs that reload them at once, never served after a change
(``web/recent.py``), and bookkeeping jobs that end with nothing to do reaching no
dashboard (``Engine._shown``)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.records import Job
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app
from auto_maintainer.web.recent import Recent

REPO = "acme/widget"


async def test_tabs_asking_at_once_share_one_answer_until_something_changes() -> None:
    events = [0]
    recent = Recent(60, lambda: events[0])
    calls = 0
    ready = asyncio.Event()

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        await ready.wait()
        return {"n": calls}

    tabs = [
        asyncio.create_task(recent.json(("prs", "maria"), compute)) for _ in range(3)
    ]
    await asyncio.sleep(0)
    ready.set()
    assert {(await t).body for t in tabs} == {b'{"n":1}'} and calls == 1
    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":1}'
    assert (await recent.json(("prs", "walt"), compute)).body == b'{"n":2}'
    events[0] += 1
    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":3}'
    recent.wrote()
    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":4}'


async def test_an_agents_progress_leaves_the_kept_lists_alone() -> None:
    """Agents report a step a second each; were each step a change, no list would
    stay kept long enough for the next tab to get it."""
    from auto_maintainer.engine.bus import EventBus

    bus = EventBus()
    recent = Recent(60, lambda: bus.count)
    calls = 0

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"n": calls}

    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":1}'
    bus.notify(Event(kind=EventKind.JOB_PROGRESS, repo=REPO, payload={"job": "j"}))
    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":1}'
    bus.notify(Event(kind=EventKind.PR_REFRESHED, repo=REPO))
    assert (await recent.json(("prs", "maria"), compute)).body == b'{"n":2}'


async def test_a_failed_answer_is_asked_again() -> None:
    recent = Recent(60, lambda: 0)
    calls = 0

    async def compute() -> list[int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("store busy")
        return [calls]

    with pytest.raises(RuntimeError):
        await recent.json("work", compute)
    assert (await recent.json("work", compute)).body == b"[2]"


async def test_a_tab_that_leaves_does_not_stop_the_others_answer() -> None:
    recent = Recent(60, lambda: 0)
    ready = asyncio.Event()

    async def compute() -> list[str]:
        await ready.wait()
        return ["ok"]

    leaving = asyncio.create_task(recent.json("work", compute))
    staying = asyncio.create_task(recent.json("work", compute))
    await asyncio.sleep(0)
    leaving.cancel()
    ready.set()
    assert (await staying).body == b'["ok"]'


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_claim_shows_in_the_next_pr_list(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        h.forge.set_permission(REPO, "walt", Permission.WRITE)
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)

        def reviewers(rows: list[dict[str, Any]]) -> list[str]:
            [row] = [r for r in rows if r["repo"] == REPO and r["number"] == 42]
            return [a["login"] for a in row["assignees"] if a["role"] == "reviewer"]

        async with _as(app, "walt") as walt:
            before = reviewers((await walt.get("/api/prs")).json())
            assert "walt" not in before
            claimed = await walt.post(f"/api/prs/{REPO}/42/claim")
            assert claimed.status_code == 200
            # The claim published no event: the change itself retires the list.
            assert "walt" in reviewers((await walt.get("/api/prs")).json())
    finally:
        await h.close()


async def test_hygiene_that_finds_nothing_new_reloads_no_dashboard(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    try:
        heard: list[str] = []
        finished: list[str] = []
        h.service.engine.on_change = lambda job: heard.append(job.playbook)

        async def on_event(event: Event) -> None:
            if event.kind == EventKind.JOB_FINISHED:
                finished.append(event.payload["playbook"])

        h.service.bus.subscribe(on_event)
        h.forge.put_pr(make_pr())
        await h.poll()
        assert "hygiene" in heard and "hygiene" in finished, (
            "a hygiene run that recorded a PR's checks reaches the PR's page"
        )
        heard.clear()
        finished.clear()
        await h.poll(reconcile=True)
        jobs: list[Job] = await h.store.list_jobs(playbooks=["hygiene"])
        assert any((j.result or {}).get("skipped") == "unchanged" for j in jobs), (
            "the reconcile pass ran hygiene again on the unchanged PR"
        )
        assert "hygiene" not in heard and "hygiene" not in finished
    finally:
        await h.close()
