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


"""Learning from the service's own trouble: a job that ran into trouble gets a look
back, which yields lessons people approve before any agent reads them, and
suggestions of changes to the service for its owner."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import TaskKind
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.memory import MemoryChangeState, repo_dir
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.progress import JobStep, steps_key
from auto_maintainer.domain.records import Job, JobStatus
from auto_maintainer.domain.retro import SUGGESTIONS_KEY, trouble
from auto_maintainer.profiles.knowledge import (
    AGENT_LESSONS,
    GLOBAL_AGENT_LESSONS,
    machine_lessons_path,
)
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
CODE = "acme/am"


def _steps(failures: int) -> list[JobStep]:
    steps = [JobStep(kind="start", runtime="agy")]
    for i in range(failures):
        steps += [
            JobStep(
                kind="tool", text="Bash", detail=f"lab -- 'apt-get install libx{i}'"
            ),
            JobStep(kind="result", ok=False, detail="E: Permission denied (sudo)"),
        ]
    steps += [
        JobStep(kind="output", text="a800 · head", runtime="a800", detail="..."),
        JobStep(kind="tool", text="Bash", detail="lab -- 'conda install libx'"),
        JobStep(kind="result", ok=True, detail="done"),
        JobStep(kind="done", ok=True, output="Installed libx with conda; reproduced."),
    ]
    return steps


def test_only_a_job_that_ran_into_trouble_is_worth_a_look() -> None:
    smooth = trouble(_steps(0))
    assert not smooth.worth_a_look
    rough = trouble(_steps(3))
    assert rough.worth_a_look and rough.machines == ["a800"]
    assert not trouble(_steps(2)).worth_a_look, "two failures are everyday work"
    searches = [
        JobStep(kind="tool", text="Bash", detail="grep -r nothing"),
        JobStep(kind="result", ok=False, detail=""),
    ] * 5
    assert not trouble(searches).worth_a_look, (
        "a search that found nothing is no trouble"
    )
    assert "FAILED Bash: lab -- 'apt-get install libx0'" in rough.digest
    assert "ok     Bash: lab -- 'conda install libx'" in rough.digest
    assert "Installed libx with conda" in rough.digest
    assert trouble([JobStep(kind="done", ok=False, text="timed out")]).worth_a_look


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = await Harness.create(tmp_path, code_repo=CODE)
    harness.forge.set_permission(REPO, "walt", Permission.WRITE)
    yield harness
    await harness.close()


async def _finish(h: Harness, failures: int, playbook: str = "reproduce") -> Job:
    job = Job(
        playbook=playbook,
        key=f"{playbook}:{REPO}#42:x{failures}",
        repo=REPO,
        number=42,
        pool="agent",
        status=JobStatus.DONE,
    )
    await h.store.upsert_job(job)
    await h.store.set_state(
        steps_key(job.id), [s.model_dump(mode="json") for s in _steps(failures)]
    )
    await h.emit(
        Event(
            kind=EventKind.JOB_FINISHED,
            repo=REPO,
            number=42,
            payload={"job": job.id, "playbook": playbook, "status": "done"},
        )
    )
    await h.settle()
    return job


def _look_back(task: Any) -> dict[str, Any]:
    return {
        "cause": "apt needs sudo on a800, which the session does not allow",
        "lessons": [
            {
                "scope": "machine",
                "machine": "a800",
                "lesson": "Install system libraries on a800 with conda, not apt: apt needs sudo.",
            },
            {
                "scope": "repo",
                "lesson": "Run requirements/install.sh before the tests.",
            },
            {
                "scope": "machine",
                "machine": "elsewhere",
                "lesson": "Not a machine it used.",
            },
        ],
        "suggestion": {
            "title": "Put libx in the agents' image",
            "problem": "Every reproduction installs libx by hand.",
            "proposal": "Add libx to agent.Dockerfile.",
        },
    }


async def test_trouble_becomes_lessons_people_approve_and_a_suggestion(
    h: Harness,
) -> None:
    looks: list[Any] = []

    def look(task: Any) -> dict[str, Any]:
        looks.append(task)
        return _look_back(task)

    h.runner.on("learn", look)
    await _finish(h, failures=0)
    assert not looks, "a job that went smoothly costs no agent"
    await _finish(h, failures=3)
    [task] = looks
    assert task.budget == "quick" and task.mode == "read_only" and task.untrusted_input
    assert "apt-get install libx0" in task.prompt
    changes = await h.store.list_memory_changes(MemoryChangeState.OPEN)
    own = next(c for c in changes if c.repos == [REPO])
    shared = next(c for c in changes if c.service)
    assert not own.from_own_signals, "a maintainer approves it before agents read it"
    assert (
        "Run requirements/install.sh" in own.files[f"{repo_dir(REPO)}/{AGENT_LESSONS}"]
    )
    assert list(shared.files) == [machine_lessons_path("a800")]
    assert "conda, not apt" in shared.files[machine_lessons_path("a800")]
    assert "Not a machine it used" not in str(shared.files)
    [suggestion] = await h.store.get_state(SUGGESTIONS_KEY)
    assert suggestion["title"] == "Put libx in the agents' image"
    assert suggestion["count"] == 1

    # The same trouble again: the same suggestion counts up, not a second one.
    def again(task: Any) -> dict[str, Any]:
        found = _look_back(task)
        found["suggestion"]["same_as"] = suggestion["id"]
        found["lessons"] = []
        return found

    h.runner.on("learn", again)
    await _finish(h, failures=4)
    [counted] = await h.store.get_state(SUGGESTIONS_KEY)
    assert counted["count"] == 2 and len(counted["jobs"]) == 2


async def test_approved_lessons_reach_the_next_agent_of_that_kind(
    h: Harness, tmp_path: Path
) -> None:
    h.memory.files[f"{repo_dir(REPO)}/{AGENT_LESSONS}"] = (
        "# Agent lessons\n- Run requirements/install.sh before the tests.\n"
    )
    h.memory.files[GLOBAL_AGENT_LESSONS] = (
        "# Agent lessons\n- `lab --sync` again after every change.\n"
    )
    lessons = await h.service.knowledge.agent_lessons(REPO)
    assert "Run requirements/install.sh" in lessons and "lab --sync" in lessons
    from auto_maintainer.playbooks.prompts import work_lessons_block

    block = work_lessons_block(lessons)
    assert "never override your task" in block and "requirements/install.sh" in block
    # A fix someone asks for reads them.
    from auto_maintainer.domain.agents import AgentResult
    from auto_maintainer.testing.fakes import make_pr

    h.forge.put_pr(make_pr())
    asked: list[Any] = []

    def fix(task: Any) -> AgentResult:
        asked.append(task)
        return AgentResult(runtime="scripted", ok=True, text="Nothing to change.")

    h.runner.on(TaskKind.FIX.value, fix)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        out = await maria.post(f"/api/prs/{REPO}/42/fix", json={"request": "Fix it."})
        assert out.status_code == 200, out.text
    await h.settle()
    assert "Run requirements/install.sh before the tests." in asked[0].prompt
    assert "lab --sync" in asked[0].prompt


@contextlib.asynccontextmanager
async def _client(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_only_the_owner_turns_a_suggestion_into_a_self_improvement(
    h: Harness, tmp_path: Path
) -> None:
    h.runner.on("learn", _look_back)
    await _finish(h, failures=3)
    [suggestion] = await h.store.get_state(SUGGESTIONS_KEY)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        assert (await walt.get("/api/learning")).json()["owner"] is False
        refused = await walt.post(
            f"/api/learning/suggestions/{suggestion['id']}/approve", json={}
        )
        assert refused.status_code == 403
    started: list[Event] = []

    async def seen(event: Event) -> None:
        started.append(event)

    h.service.bus.subscribe(seen)
    async with _client(app, "maria") as maria:
        view = (await maria.get("/api/learning")).json()
        assert view["owner"] and view["suggestions"][0]["id"] == suggestion["id"]
        assert [c["files"] for c in view["lessons"]] == [
            {
                machine_lessons_path("a800"): view["lessons"][0]["files"][
                    machine_lessons_path("a800")
                ]
            }
        ]
        out = await maria.post(
            f"/api/learning/suggestions/{suggestion['id']}/approve",
            json={"text": "Add libx to agent.Dockerfile, pinned."},
        )
        assert out.status_code == 200, out.text
    await h.settle()
    [improve] = [e for e in started if e.payload.get("command") == "improve"]
    assert improve.payload["request"] == "Add libx to agent.Dockerfile, pinned."
    assert improve.payload["approved_by"] == "maria"
    [decided] = await h.store.get_state(SUGGESTIONS_KEY)
    assert decided["state"] == "approved"


async def test_a_repository_gets_a_bounded_number_of_looks_a_day(h: Harness) -> None:
    from auto_maintainer.domain.retro import PER_DAY

    looks: list[Any] = []
    h.runner.on("learn", lambda t: looks.append(t) or {"lessons": []})
    await h.store.set_state(
        f"retrospect-count:{REPO}:{h.clock.now():%Y-%m-%d}", PER_DAY
    )
    await _finish(h, failures=5)
    assert not looks, "trouble past the day's looks waits for tomorrow"
