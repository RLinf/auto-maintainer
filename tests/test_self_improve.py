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
from auto_maintainer.domain.agents import AgentResult, AgentTask
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


def _implement(task: AgentTask) -> AgentResult | dict:
    """The agent edits the worktree and ends without any JSON, as Antigravity does on
    long tasks; a second, read-only task describes the diff."""
    assert task.repo == CODE and "Add a ping endpoint" in task.prompt
    if task.mode == "read_only":
        assert "ping.py" in task.prompt, "the summary is written from the diff"
        return {"title": "add a ping endpoint", "summary": "Adds ping()."}
    assert task.mode == "workspace_write" and "AGENTS.md" in task.prompt
    target = Path(task.workdir) / "src" / "ping.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def ping() -> str:\n    return 'pong'\n")
    return AgentResult(runtime="scripted", ok=True, text="Added ping(); pytest passed.")


async def test_a_chat_request_waits_for_approval_then_becomes_a_pr(
    h: Harness, tmp_path: Path
) -> None:
    h.runner.on(
        "chat",
        lambda task: {
            "reply": "I can change my code for that once you approve.",
            "proposals": [{"kind": "improve", "request": "Add a ping endpoint."}],
        },
    )
    h.runner.on("self_improve", _implement)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        thread = (await walt.post("/api/chat/threads", json={"text": "ping"})).json()
        out = (
            await walt.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Can you answer pings?"},
            )
        ).json()
        [card] = out["requests"]
        await h.settle()
        assert h.service.a.publisher.published == [], "nothing starts before approval"  # type: ignore[union-attr]
        denied = await walt.post(
            f"/api/chat/threads/{thread['id']}/requests/{card['id']}/approve"
        )
        assert denied.status_code == 403, (
            "write access is not enough to change the service"
        )

    async with _client(app, "maria") as maria:
        # A maintainer of the service's repository finds walt's card and approves it.
        [waiting] = (await maria.get("/api/chat/requests")).json()
        assert waiting["asked_by"] == "walt" and waiting["can_decide"] is True
        base = f"/api/chat/threads/{waiting['thread_id']}"
        approved = (
            await maria.post(f"{base}/requests/{waiting['request']['id']}/approve")
        ).json()
        [decided] = approved["requests"]
        assert decided["state"] == "approved" and decided["job"]
        assert decided["decided_by"] == "maria"
        await h.settle()
    async with _client(app, "walt") as walt:
        messages = (await walt.get(f"{base}/messages")).json()

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
    assert "Requested by walt in chat, approved to start by maria" in pr["body"]
    assert "must approve the merge" in pr["body"]
    assert "Added ping(); pytest passed." in pr["body"], "the agent's account is kept"
    assert TESTS in h.sandbox.runs and LINT in h.sandbox.runs
    assert any("I opened" in m["text"] for m in messages if m["role"] == "assistant")


async def test_a_conversation_runs_one_code_change_at_a_time(
    h: Harness, tmp_path: Path
) -> None:
    h.runner.on(
        "chat",
        lambda task: {
            "reply": "Here is the request.",
            "proposals": [{"kind": "improve", "request": "Add a ping endpoint."}],
        },
    )
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        thread = (await maria.post("/api/chat/threads", json={"text": "ping"})).json()
        base = f"/api/chat/threads/{thread['id']}"
        first = (
            await maria.post(f"{base}/messages", json={"text": "Answer pings."})
        ).json()
        waiting = (await maria.post(f"{base}/messages", json={"text": "Again."})).json()
        assert waiting["requests"] == [] and "waiting for approval" in waiting["text"]
        approved = (
            await maria.post(f"{base}/requests/{first['requests'][0]['id']}/approve")
        ).json()
        job_id = approved["requests"][0]["job"]
        await h.service.bus.drain()
        again = (await maria.post(f"{base}/messages", json={"text": "And?"})).json()
        job = await maria.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200, job.text
    assert again["jobs"] == [job_id] and "did not ask for another" in again["text"]
    assert job.json()["playbook"] == "self_improve"
    improves = [
        j for j in await h.store.list_jobs(limit=1000) if j.playbook == "self_improve"
    ]
    assert len(improves) == 1


async def test_no_change_means_no_pr(h: Harness, tmp_path: Path) -> None:
    from auto_maintainer.domain.events import Event, EventKind

    h.runner.on(
        "self_improve",
        lambda task: AgentResult(
            runtime="scripted", ok=True, text="That is not a code change."
        ),
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


async def test_the_agent_offers_to_add_a_repository_and_a_maintainer_approves(
    h: Harness, tmp_path: Path
) -> None:
    from auto_maintainer.domain.snapshot import RepoInfo

    h.forge.add_repo(RepoInfo(name="acme/gadget", default_branch_sha="f" * 40))
    h.forge.set_permission("acme/gadget", "maria", Permission.MAINTAIN)
    h.runner.on(
        "chat",
        lambda task: {
            "reply": "I can add it.",
            "proposals": [{"kind": "add_repo", "repo": "acme/gadget"}],
        },
    )
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        thread = (await maria.post("/api/chat/threads", json={"text": "hi"})).json()
        base = f"/api/chat/threads/{thread['id']}"
        out = (await maria.post(f"{base}/messages", json={"text": "Add gadget"})).json()
        [card] = out["requests"]
        assert card["kind"] == "add_repo" and card["text"] == "acme/gadget"
        assert await h.store.get_repo("acme/gadget") is None, "nothing before approval"
        decided = (await maria.post(f"{base}/requests/{card['id']}/approve")).json()
        again = await maria.post(f"{base}/requests/{card['id']}/approve")
    assert decided["requests"][0]["state"] == "approved"
    assert again.status_code == 409
    assert await h.store.get_repo("acme/gadget") is not None


async def test_a_conversation_outside_any_repository_reads_the_services_own_code(
    h: Harness, tmp_path: Path
) -> None:
    h.runner.on(
        "chat", lambda task: {"reply": "It reads the settings file.", "proposals": []}
    )
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        thread = (await maria.post("/api/chat/threads", json={"text": "hi"})).json()
        await maria.post(
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "How do you pick the agent for a review?"},
        )
    [task] = [t for t in h.runner.tasks if str(t.kind) == "chat"]
    running = (await h.service.a.updater.status()).running_sha  # type: ignore[union-attr]
    assert (CODE, None, running) in h.service.a.workspace.prepared  # type: ignore[attr-defined]
    assert Path(task.workdir).name.startswith(CODE.replace("/", "__"))
    assert "a checkout of the service's own code" in task.prompt
