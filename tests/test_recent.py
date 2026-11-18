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


async def test_a_shared_value_is_computed_once_for_everyone_asking() -> None:
    """Tabs asking at the same moment share one computation, which pages extend with
    each viewer's own fields; a new event or a change computes it again."""
    from auto_maintainer.web.recent import Recent

    events = [0]
    recent = Recent(60.0, lambda: events[0])
    calls = [0]

    async def compute() -> dict[str, int]:
        calls[0] += 1
        await asyncio.sleep(0.01)
        return {"n": calls[0]}

    first = await asyncio.gather(*(recent.shared("k", compute) for _ in range(5)))
    assert calls[0] == 1 and all(v == {"n": 1} for v in first)
    assert await recent.shared("k", compute) == {"n": 1}, "kept within its window"
    events[0] += 1
    assert await recent.shared("k", compute) == {"n": 2}, "an event retires it"
    recent.wrote()
    assert await recent.shared("k", compute) == {"n": 3}, "and so does a change"
    assert await recent.shared("k", compute, seconds=0) == {"n": 4}, "its own window"


async def test_a_jobs_view_is_shared_but_its_viewers_fields_are_their_own(
    tmp_path: Path,
) -> None:
    from auto_maintainer.domain.records import Job, JobStatus
    from auto_maintainer.testing.demo import DEMO_REPO, build_demo
    from auto_maintainer.web.app import create_app

    h = await build_demo(tmp_path)
    try:
        h.forge.set_permission(DEMO_REPO, "walt", Permission.WRITE)
        job, _ = await h.store.upsert_job(
            Job(
                id="job_shared",
                key="review:t",
                playbook="review",
                repo=DEMO_REPO,
                number=42,
                status=JobStatus.RUNNING,
                events=[
                    {
                        "id": "evt_req",
                        "kind": "dashboard.command",
                        "payload": {"by": "maria"},
                    }
                ],
            )
        )
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with _as(app, "maria") as maria, _as(app, "walt") as walt:
            hers = (await maria.get("/api/jobs/job_shared")).json()
            his = (await walt.get("/api/jobs/job_shared")).json()
            assert hers["mine"] and not his["mine"]
            assert "_asked" not in hers and "_asked" not in his
            by_request = (await maria.get("/api/jobs/evt_req")).json()
            assert by_request["id"] == "job_shared", "a request's id finds its job"
    finally:
        await h.close()


async def test_a_jobs_new_step_shows_at_once_though_its_view_is_shared(
    tmp_path: Path,
) -> None:
    """Steps are announced once a second, as long as a view is shared: a view kept
    by its time alone would show each step one notice late, and the last one of a
    burst not until the agent's next."""
    from auto_maintainer.domain.labs import LabProgress
    from auto_maintainer.domain.records import JobStatus
    from auto_maintainer.testing.demo import DEMO_REPO
    from auto_maintainer.web import jobs

    h = await build_demo(tmp_path)
    try:
        await h.store.upsert_job(
            Job(
                id="job_steps",
                key="review:s",
                playbook="review",
                repo=DEMO_REPO,
                number=42,
                status=JobStatus.RUNNING,
                events=[{"id": "evt_s", "payload": {"by": "maria"}}],
            )
        )
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        jobs.JOB_VIEW_SECONDS, kept = 60.0, jobs.JOB_VIEW_SECONDS
        try:
            async with _as(app, "maria") as maria:
                before = (await maria.get("/api/jobs/job_steps")).json()
                await h.service.progress.lab(
                    "job_steps", DEMO_REPO, 42, "a800", LabProgress(phase="cloning")
                )
                after = (await maria.get("/api/jobs/job_steps")).json()
        finally:
            jobs.JOB_VIEW_SECONDS = kept
        assert [s["text"] for s in after["steps"]] == [
            *(s["text"] for s in before["steps"]),
            "cloning",
        ]
    finally:
        await h.close()


async def test_kept_values_in_their_window_outlast_a_full_cache() -> None:
    """A watched job adds a shared key a step: filling the cache drops what is out
    of its window, not the PR list everyone is reading."""
    recent = Recent(60.0, lambda: 0)
    await recent.shared("rows", lambda: _value("rows"), seconds=60.0)
    for i in range(300):
        await recent.shared(("step", i), lambda i=i: _value(i), seconds=0.0)
    assert await recent.shared("rows", lambda: _value("again")) == "rows"


async def _value(v: Any) -> Any:
    return v
