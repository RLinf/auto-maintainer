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

"""Self-improvement: a reviewer's request in chat becomes a PR on the service's code,
which only a maintainer of that repository can merge and deploy."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from auto_maintainer.domain.actions import ActionKind, ActionStatus
from auto_maintainer.domain.agents import AgentTask
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.playbooks.self_improve import LINT, TESTS
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app

CODE = "acme/am"


@pytest.fixture
async def h(tmp_path: Path):
    harness = await Harness.create(tmp_path, code_repo=CODE)
    harness.forge.set_permission(CODE, "walt", Permission.WRITE)
    harness.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    yield harness
    await harness.close()


@contextlib.asynccontextmanager
async def _client(app, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


def _implement(task: AgentTask) -> dict:
    target = Path(task.workdir) / "src" / "ping.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def ping() -> str:\n    return 'pong'\n")
    assert "Add a ping endpoint" in task.prompt and "AGENTS.md" in task.prompt
    assert task.mode == "workspace_write" and task.repo == CODE
    return {
        "done": True,
        "title": "add a ping endpoint",
        "summary": "Adds ping().",
        "tests": "uv run pytest -q: 220 passed",
    }


async def test_a_chat_request_becomes_a_pr(h: Harness, tmp_path: Path) -> None:
    h.runner.on(
        "chat",
        lambda task: {
            "reply": "I will change my code for that.",
            "proposals": [{"kind": "improve", "request": "Add a ping endpoint."}],
        },
    )
    h.runner.on("self_improve", _implement)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        thread = (await walt.post("/api/chat/threads", json={"text": "ping"})).json()
        out = await walt.post(
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "Can you answer pings?"},
        )
        assert out.status_code == 200, out.text
        await h.settle()
        messages = (await walt.get(f"/api/chat/threads/{thread['id']}/messages")).json()

    [published] = h.service.a.publisher.published  # type: ignore[union-attr]
    assert published["repo"] == CODE and published["base"] == "b" * 40
    assert published["branch"].startswith("am/self/add-a-ping-endpoint-")
    assert (
        "ping.py" in published["patch"] and "Requested-by: walt" in published["message"]
    )
    [pr] = h.forge.writes_of("create_service_pr")
    assert (
        pr["repo"] == CODE
        and pr["base"] == "main"
        and pr["head"] == published["branch"]
    )
    assert pr["title"] == "feat: add a ping endpoint"
    assert "Requested by walt" in pr["body"] and "must approve the merge" in pr["body"]
    assert TESTS in h.sandbox.runs and LINT in h.sandbox.runs
    assert any("I opened" in m["text"] for m in messages if m["role"] == "assistant")


async def test_no_change_means_no_pr(h: Harness, tmp_path: Path) -> None:
    from auto_maintainer.domain.events import Event, EventKind

    h.runner.on(
        "self_improve",
        lambda task: {
            "done": False,
            "title": "x",
            "summary": "Not a code change.",
            "tests": "",
        },
    )
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            payload={"command": "improve", "request": "x"},
        )
    )
    assert h.forge.writes_of("create_service_pr") == []
    assert h.service.a.publisher.published == []  # type: ignore[union-attr]


async def test_only_code_maintainers_merge_and_deploy(
    h: Harness, tmp_path: Path
) -> None:
    h.forge.put_pr(make_pr(repo=CODE, number=7, head_ref="am/self/ping-1"))
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        view = (await walt.get("/api/service")).json()
        assert view["can_approve"] is False and "maintain" in view["reason"]
        assert view["prs"][0]["self_improvement"] is True
        assert (await walt.post("/api/service/prs/7/merge")).status_code == 403
        assert (
            await walt.post("/api/service/deploy", json={"sha": "f" * 40})
        ).status_code == 403
    async with _client(app, "stranger") as stranger:
        assert (await stranger.get("/api/service")).status_code == 403
    async with _client(app, "maria") as maria:
        assert (await maria.get("/api/service")).json()["can_approve"] is True
        merged = await maria.post("/api/service/prs/7/merge")
        assert merged.status_code == 200, merged.text
        await h.settle()
        assert h.forge.writes_of("merge_service_pr")[0]["number"] == 7
        deployed = await maria.post("/api/service/deploy", json={"sha": "f" * 40})
        assert deployed.status_code == 200, deployed.text
        await h.settle()
    assert h.service.a.updater.deployed == ["f" * 40]  # type: ignore[union-attr]
    assert h.service.restart_requested.is_set()
    records = await h.store.list_actions(repo=CODE, kinds=[ActionKind.SERVICE_DEPLOY])
    assert records[0].status == ActionStatus.EXECUTED
    assert records[0].action.provenance.requested_by == "maria"


async def test_a_failed_deploy_keeps_the_service_running(
    h: Harness, tmp_path: Path
) -> None:
    h.service.a.updater.fail = True  # type: ignore[union-attr]
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        await maria.post("/api/service/deploy", json={"sha": "f" * 40})
        await h.settle()
    assert not h.service.restart_requested.is_set()
    [record] = await h.store.list_actions(repo=CODE, kinds=[ActionKind.SERVICE_DEPLOY])
    assert record.status == ActionStatus.FAILED and "tests failed" in (
        record.error or ""
    )
