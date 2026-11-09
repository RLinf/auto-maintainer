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

"""Work a restart cut off goes on (design 4.10): the agent's session is kept while
its run is unfinished, and the job's next run resumes it on the same agent, in the
same session on the test machine."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    TaskKind,
)
from auto_maintainer.domain.progress import agent_session_key
from auto_maintainer.domain.records import JobStatus
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
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


async def test_an_unfinished_runs_session_is_kept_until_it_ends(demo) -> None:
    h, _ = demo
    task = AgentTask(
        kind=TaskKind.REVIEW, prompt="p", workdir=Path("/tmp"), job_id="job_1"
    )
    progress = h.service.progress
    key = agent_session_key("job_1", "review")
    await progress.observe(
        task,
        "antigravity",
        AgentEvent(type=AgentEventType.SESSION, data={"session": "s1"}),
    )
    saved = await h.store.get_state(key)
    assert saved and (saved["runtime"], saved["session"]) == ("antigravity", "s1")
    await progress.observe(task, "antigravity", AgentEvent(type=AgentEventType.FINAL))
    assert not await h.store.get_state(key), "a run that ended leaves nothing to resume"


def _review(task: AgentTask) -> Any:
    return {"summary": "Fine.", "recommendation": "Ready"}


async def _cut_off_review(h: Any, app: Any, runtime: str = "antigravity") -> str:
    """Ask for a review, and record that a restart cut off its agent's session."""
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
    await h.service.bus.drain()
    [job] = [
        j for j in await h.store.list_jobs([JobStatus.QUEUED]) if j.playbook == "review"
    ]
    await h.store.set_state(
        agent_session_key(job.id, "review"), {"runtime": runtime, "session": "s-before"}
    )
    return job.id


async def test_a_review_a_restart_cut_off_resumes_its_agent(demo) -> None:
    h, app = demo
    h.runner.on("review", _review)
    await _cut_off_review(h, app)
    await h.settle()
    [review] = [t for t in h.runner.tasks if t.kind == "review"]
    route = h.runner.routes[h.runner.tasks.index(review)]
    assert review.resume_session == "s-before" and route.primary == "antigravity"
    assert review.prompt.startswith("The service restarted while you were working")
    assert "Your session on the test machine is still open" in review.prompt
    assert "Test and reproduce the PR" in review.prompt, "the whole task follows"


async def test_an_agent_that_cannot_resume_starts_afresh(demo) -> None:
    h, app = demo

    def review(task: AgentTask) -> Any:
        if task.resume_session:
            return AgentResult(
                runtime="antigravity",
                ok=False,
                error="no such session",
                error_kind="other",
            )
        return _review(task)

    h.runner.on("review", review)
    await _cut_off_review(h, app)
    await h.settle()
    resumed, fresh = [t for t in h.runner.tasks if t.kind == "review"]
    assert resumed.resume_session == "s-before"
    assert fresh.resume_session is None
    assert fresh.prompt.startswith("An earlier agent worked on this task"), (
        "it looks at what the session's folders keep before repeating a step"
    )
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    assert job.status == "done"
