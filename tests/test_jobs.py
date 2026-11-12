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

"""Agents' work, live: the steps a job's agent takes, who may see and stop it, and
what reaches the dashboard."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.agents.router import AgentRouter
from auto_maintainer.agents.scripted import ScriptedAgentRuntime
from auto_maintainer.domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    TaskKind,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.progress import FINAL_OUTPUT_LIMIT, step_from_event
from auto_maintainer.domain.records import JobStatus
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


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


def _tool(name: str, **inputs: str) -> AgentEvent:
    return AgentEvent(
        type=AgentEventType.TOOL_CALL, text=name, data={"name": name, "input": inputs}
    )


# -- steps ---------------------------------------------------------------------------


def test_a_step_says_what_the_agent_did_without_credentials() -> None:
    tool = step_from_event(_tool("Bash", command="pytest -q tests/"), "codex", "review")
    assert (
        tool is not None and tool.kind == "tool" and tool.detail == "pytest -q tests/"
    )
    secret = "ghp_" + "a" * 36
    said = step_from_event(
        AgentEvent(type=AgentEventType.MESSAGE, text=f"the token is {secret}"),
        "codex",
        "review",
    )
    assert said is not None and secret not in said.text
    assert (
        step_from_event(AgentEvent(type=AgentEventType.USAGE), "codex", "review")
        is None
    )


def test_a_final_message_is_kept_apart_from_the_error() -> None:
    secret = "ghp_" + "d" * 36
    step = step_from_event(
        AgentEvent(
            type=AgentEventType.FINAL,
            text="The null check is wrong. " + secret + " " + "x" * 9000,
            result=AgentResult(runtime="codex", ok=False, error="stopped early"),
        ),
        "codex",
        "review",
    )
    assert step is not None and step.kind == "done"
    assert step.text == "stopped early"
    assert step.output.startswith("The null check is wrong.")
    assert secret not in step.output
    assert len(step.output) <= FINAL_OUTPUT_LIMIT


async def test_the_router_reports_each_attempt_and_survives_its_observer() -> None:
    secret = "ghp_" + "b" * 36
    runtime = ScriptedAgentRuntime(
        "a",
        [
            [
                AgentEvent(type=AgentEventType.MESSAGE, text=f"reading {secret}"),
                _tool("Read", file_path="src/loader.py"),
                AgentEvent(
                    type=AgentEventType.FINAL,
                    result=AgentResult(runtime="a", ok=True, text="done"),
                ),
            ]
        ],
    )
    router = AgentRouter({"a": runtime})
    seen: list[tuple[str, AgentEvent]] = []

    async def observe(task: AgentTask, name: str, event: AgentEvent) -> None:
        seen.append((name, event))
        raise RuntimeError("a broken observer")

    router.bind_progress(observe)
    task = AgentTask(kind=TaskKind.REVIEW, prompt="p", workdir=Path("/tmp"))
    result = await router.run(task, Route(primary="a"))
    assert result.ok
    assert [e.type for _, e in seen] == [
        AgentEventType.STARTED,
        AgentEventType.MESSAGE,
        AgentEventType.TOOL_CALL,
        AgentEventType.FINAL,
    ]
    assert secret not in seen[1][1].text


# -- the job view --------------------------------------------------------------------


async def test_the_requester_follows_their_review_step_by_step(demo) -> None:
    h, app = demo
    h.runner.steps["review"] = [
        AgentEvent(type=AgentEventType.MESSAGE, text="Reading the diff."),
        _tool("Bash", command="pytest -q tests/test_loader.py"),
    ]
    async with _as(app, "maria") as maria:
        started = await maria.post(
            "/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]}
        )
        assert started.json()[0]["run"] == "review"
        await h.settle()
        work = (await maria.get("/api/work")).json()
        [mine] = [j for j in work if j["playbook"] == "review"]
        assert mine["playbook"] == "review" and mine["by"] == "maria" and mine["mine"]
        assert mine["agent"] == "claude-code" and mine["status"] == "done"
        assert [p["kind"] for p in mine["produced"]].count("review.post") == 1
        job = (await maria.get(f"/api/jobs/{mine['id']}")).json()
        kinds = [s["kind"] for s in job["steps"]]
        start = kinds.index("start")
        before = [(s["kind"], s.get("text", "")) for s in job["steps"][:start]]
        assert ("message", "Copying head") in before, (
            "the review's session on the test machine opens before its agent starts"
        )
        assert "tool" in kinds[start:] and kinds[-1] == "done"
        assert any("pytest" in s["detail"] for s in job["steps"])
        # What the agent said when it finished shows with the job, and on the job
        # alone: the review card reads it from there rather than the step list.
        assert mine["final"] == job["final"]
        assert "steps" not in mine and job["final"]
    async with _as(app, "walt") as walt:
        assert (await walt.get("/api/work")).json() == []
        everyone = (await walt.get("/api/work?everyone=true")).json()
        [theirs] = [j for j in everyone if j["playbook"] == "review"]
        assert theirs["id"] == mine["id"] and not theirs["mine"]
    async with _as(app, "eve") as eve:
        assert (await eve.get(f"/api/jobs/{mine['id']}")).status_code == 403


async def test_a_job_is_stopped_by_its_requester_or_a_maintainer(demo) -> None:
    h, app = demo
    h.runner.steps["review"] = [
        # Long enough to be stopped midway however slow the machine running the tests.
        AgentEvent(type=AgentEventType.MESSAGE, text=f"step {i}")
        for i in range(400)
    ]
    h.runner.step_delay = 0.05
    async with _as(app, "walt") as walt:
        await walt.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 43}]})
        await h.service.bus.drain()
        [job] = [
            j
            for j in await h.store.list_jobs([JobStatus.QUEUED])
            if j.playbook == "review"
        ]
        [claimed] = await h.store.claim_jobs("agent", 1, 600, h.clock.now())
        running = asyncio.create_task(h.service.engine.run_job(claimed))
        for _ in range(100):
            if len(await h.service.progress.steps(job.id)) > 2:
                break
            await asyncio.sleep(0.02)
        async with _as(app, "bob") as bob:
            h.forge.set_permission(REPO, "bob", Permission.WRITE)
            refused = await bob.post(f"/api/jobs/{job.id}/stop")
            assert refused.status_code == 403
        stopped = (await walt.post(f"/api/jobs/{job.id}/stop")).json()
        await running
    assert stopped["status"] == "cancelled" and stopped["error"] == "stopped by walt"
    assert not [
        p
        for p in await h.store.list_proposals()
        if (await h.store.get_action(p.action_id)).action.number == 43  # type: ignore[union-attr]
        and p.requested_by == "walt"
    ]

    await walt_queue_and_maintainer_stops(h, app)


async def walt_queue_and_maintainer_stops(h: Any, app: Any) -> None:
    async with _as(app, "walt") as walt:
        await walt.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 44}]})
    await h.service.bus.drain()
    [queued] = [
        j
        for j in await h.store.list_jobs([JobStatus.QUEUED])
        if j.playbook == "review" and j.number == 44
    ]
    async with _as(app, "maria") as maria:
        stopped = (await maria.post(f"/api/jobs/{queued.id}/stop")).json()
    assert stopped["status"] == "cancelled" and stopped["error"] == "stopped by maria"


async def test_a_prs_page_lists_everyones_work_on_that_pr(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post(
            "/api/reviews",
            json={"prs": [{"repo": REPO, "number": 42}, {"repo": REPO, "number": 43}]},
        )
        await h.settle()
    async with _as(app, "walt") as walt:
        await walt.post(f"/api/prs/{REPO}/42/fix", json={})
        await h.settle()
        on_42 = (
            await walt.get(f"/api/work?repo={REPO}&number=42&everyone=true")
        ).json()
        assert {("review", "maria"), ("fix", "walt")} <= {
            (j["playbook"], j["by"]) for j in on_42
        }
        assert all(j["number"] == 42 for j in on_42)
        # a playbook that took the same request and skipped without an agent is no work
        assert not [j for j in on_42 if (j["result"] or {}).get("skipped")]
    async with _as(app, "eve") as eve:
        refused = await eve.get(f"/api/work?repo={REPO}&number=42&everyone=true")
        assert refused.status_code == 403


async def test_scoring_someone_asked_for_is_agent_work_and_quiet_rescoring_is_not(
    demo,
) -> None:
    from auto_maintainer.domain.events import Event, EventKind

    h, app = demo
    h.runner.steps["evaluate"] = [
        AgentEvent(type=AgentEventType.MESSAGE, text="Weighing the rubric.")
    ]
    async with _as(app, "maria") as maria:
        prs = [{"repo": REPO, "number": 42}, {"repo": REPO, "number": 43}]
        await maria.post("/api/evaluations", json={"prs": prs})
        await h.settle()
        snapshot = await h.store.get_snapshot(REPO, 42)
        assert snapshot is not None
        # scored a moment ago and unchanged since: only the total is recomputed
        await h.emit(
            Event(
                kind=EventKind.PR_UPDATED,
                repo=REPO,
                number=42,
                sha=snapshot.head_sha,
            )
        )
        scoring = (
            await maria.get(f"/api/work?repo={REPO}&playbook=evaluate&everyone=true")
        ).json()
        asked = [j for j in scoring if j["by"] == "maria"]
        assert sorted(j["number"] for j in asked) == [42, 43]
        assert all(j["status"] == "done" and j["result"]["tier"] for j in asked)
        assert asked[0]["batch"] and {j["batch"] for j in asked} == {asked[0]["batch"]}
        job = (await maria.get(f"/api/jobs/{asked[0]['id']}")).json()
        assert any(s["text"] == "Weighing the rubric." for s in job["steps"])
        [quiet] = [
            j
            for j in await h.store.list_jobs(repo=REPO)
            if j.playbook == "evaluate" and j.events[0]["kind"] == "pr.updated"
        ]
        assert quiet.result and quiet.result["scored"] is False
        assert quiet.id not in {j["id"] for j in scoring}
        mine = (await maria.get("/api/work")).json()
        assert {j["id"] for j in mine} == {j["id"] for j in asked}


async def test_a_job_the_service_stops_runs_again_after_it_starts(demo) -> None:
    h, app = demo
    engine = h.service.engine
    h.runner.steps["review"] = [
        AgentEvent(type=AgentEventType.MESSAGE, text=f"step {i}") for i in range(40)
    ]
    h.runner.step_delay = 0.05
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 43}]})
    await h.service.bus.drain()
    [job] = [
        j for j in await h.store.list_jobs([JobStatus.QUEUED]) if j.playbook == "review"
    ]
    engine.start()
    for _ in range(300):
        # The agent is at work (its session on the test machine opened first).
        steps = await h.service.progress.steps(job.id)
        if any(s.text.startswith("step ") for s in steps):
            break
        await asyncio.sleep(0.02)
    await engine.stop()  # a deploy
    stopped = await h.store.get_job(job.id)
    assert stopped is not None and stopped.status == JobStatus.QUEUED
    assert stopped.attempts == 0, "the stop does not count as an attempt"

    h.runner.step_delay = 0
    engine.start()
    for _ in range(300):
        done = await h.store.get_job(job.id)
        if done is not None and done.status == JobStatus.DONE:
            break
        await asyncio.sleep(0.02)
    await engine.stop()
    assert done is not None and done.status == JobStatus.DONE, done
    drafts = [
        r
        for r in await h.store.list_actions(repo=REPO, number=43, kinds=["review.post"])
        if r.action.provenance.job_id == job.id
    ]
    assert len(drafts) == 1


async def test_jobs_a_crashed_process_left_running_are_queued_at_start(demo) -> None:
    from datetime import timedelta

    from auto_maintainer.domain.records import Job

    h, _ = demo
    now = h.clock.now()
    left = Job(
        playbook="review",
        key="review:acme/widget#43:left",
        repo=REPO,
        number=43,
        pool="agent",
        status=JobStatus.RUNNING,
        attempts=1,
        lease_until=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    spent = left.model_copy(
        update={"id": "job_spent", "key": "review:acme/widget#43:spent", "attempts": 3}
    )
    for job in (left, spent):
        stored, _ = await h.store.upsert_job(job)
        await h.store.save_job(job.model_copy(update={"id": stored.id}))
    assert await h.service.engine.recover() == 2
    again = await h.store.get_job(left.id)
    assert again is not None and again.status == JobStatus.QUEUED
    assert again.attempts == 1 and again.lease_until is None
    out = await h.store.get_job("job_spent")
    assert out is not None and out.status == JobStatus.FAILED
