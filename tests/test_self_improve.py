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
from auto_maintainer.domain.ports import CommandResult
from auto_maintainer.playbooks.self_improve import FORMAT, LINT, MAX_REPAIRS, TESTS
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
    [work] = [
        t
        for t in h.runner.tasks
        if t.kind == "self_improve" and t.mode == "workspace_write"
    ]
    assert not work.untrusted_input, "a bypass the agent's settings allow applies"
    assert "walt: Can you answer pings?" in work.prompt
    assert "I can change my code" not in work.prompt, (
        "the agent's answers, which can repeat PR text, are not passed on"
    )


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


def _failing(output: str) -> CommandResult:
    return CommandResult(exit_code=1, stdout=output)


async def _improve(h: Harness, thread_id: str | None = None) -> None:
    from auto_maintainer.domain.events import Event, EventKind

    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            payload={
                "command": "improve",
                "request": "Add a ping endpoint.",
                "by": "walt",
                "thread_id": thread_id,
            },
        )
    )


async def test_failing_checks_go_back_to_the_same_agent_session(h: Harness) -> None:
    def work(task: AgentTask) -> AgentResult | dict:
        if task.mode == "read_only":
            return {"title": "add a ping endpoint", "summary": "Adds ping()."}
        target = Path(task.workdir) / "src" / "ping.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def ping() -> str:\n    return 'pong'\n")
        return AgentResult(
            runtime="codex", ok=True, text="Added ping().", session="sess-1"
        )

    h.runner.on("self_improve", work)
    # The first round: the tests fail and Ruff passes; the second: all pass.
    h.sandbox.results = [
        _failing("FAILED tests/test_ping.py::test_ping"),
        *[CommandResult(exit_code=0)] * 5,
    ]
    await _improve(h)

    edits = [
        t for t in h.runner.tasks if t.kind == "self_improve" and t.mode != "read_only"
    ]
    assert len(edits) == 2
    first, repair = edits
    assert first.resume_session is None
    assert repair.resume_session == "sess-1", "the agent goes on in its own session"
    assert {t.requested_by for t in edits} == {"walt"}, (
        "walt's tokens, not the service's"
    )
    assert "FAILED tests/test_ping.py::test_ping" in repair.prompt
    assert "Add a ping endpoint." in repair.prompt
    route = h.runner.routes[h.runner.tasks.index(repair)]
    assert route.primary == "codex" and "codex" not in route.fallback
    assert h.sandbox.runs.count(TESTS) == 2 and FORMAT in h.sandbox.runs
    assert all(h.sandbox.untrusted), "the agent's code runs only where agents run"
    [pr] = h.forge.writes_of("create_service_pr")
    assert "tests: `" in pr["body"] and "failed" not in pr["body"].split("## Checks")[1]


async def test_checks_that_keep_failing_open_no_pr(h: Harness, tmp_path: Path) -> None:
    def work(task: AgentTask) -> AgentResult:
        (Path(task.workdir) / "ping.py").write_text("def ping(): ...\n")
        return AgentResult(runtime="codex", ok=True, text="Tried.", session="s")

    h.runner.on("self_improve", work)
    h.sandbox.results = [
        CommandResult(exit_code=0),
        _failing("E501 line too long"),
        CommandResult(exit_code=0),
    ] * (MAX_REPAIRS + 1)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        thread = (await walt.post("/api/chat/threads", json={"text": "ping"})).json()
        await _improve(h, thread["id"])
        messages = (await walt.get(f"/api/chat/threads/{thread['id']}/messages")).json()

    assert h.forge.writes_of("create_service_pr") == []
    assert h.service.a.publisher.published == []  # type: ignore[union-attr]
    edits = [t for t in h.runner.tasks if t.kind == "self_improve"]
    assert len(edits) == 1 + MAX_REPAIRS, "no summary task for a change that fails"
    [told] = [m["text"] for m in messages if m["role"] == "assistant"]
    assert "I did not open a PR" in told and "lint" in told and "E501" in told
    assert "ping.py" in told
    [job] = [
        j for j in await h.store.list_jobs(limit=100) if j.playbook == "self_improve"
    ]
    assert job.status == "failed" and "lint" in (job.error or "")


async def test_only_code_maintainers_merge_and_deploy(
    h: Harness, tmp_path: Path
) -> None:
    h.forge.put_pr(make_pr(repo=CODE, number=7, head_ref="am/self/ping-1"))
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        view = (await walt.get("/api/service")).json()
        assert view["can_approve"] is False and "owns this service" in view["reason"]
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
    h.forge.set_permission("acme/gadget", "maria", Permission.ADMIN)
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


async def test_clean_self_pr_updates_its_branch_through_the_forge(
    h: Harness, tmp_path: Path
) -> None:
    h.forge.put_pr(make_pr(repo=CODE, number=7, head_ref="am/self/ping-1"))
    h.forge.put_pr(make_pr(repo=CODE, number=8, head_ref="feature/other"))
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "walt") as walt:
        assert (await walt.post("/api/service/prs/7/merge-base")).status_code == 403
    async with _client(app, "maria") as maria:
        view = (await maria.get("/api/service")).json()
        self_pr = next(p for p in view["prs"] if p["number"] == 7)
        assert self_pr["conflicting"] is False and self_pr["base"] == "main"
        assert (await maria.post("/api/service/prs/8/merge-base")).status_code == 409
        res = await maria.post("/api/service/prs/7/merge-base")
        assert res.status_code == 200, res.text
        out = res.json()
        await h.settle()
        after = (await maria.get("/api/service")).json()
    assert out["run"] == "update"
    [update] = h.forge.writes_of("update_branch")
    assert update["repo"] == CODE and update["number"] == 7
    assert any(
        row["kind"] == "code.update" and row["status"] == "executed"
        for row in after["history"]
    )


async def test_conflicting_self_pr_resolves_docs_conflict_and_pushes_after_approval(
    h: Harness, tmp_path: Path
) -> None:
    from auto_maintainer.domain.snapshot import Mergeable

    h.forge.put_pr(
        make_pr(
            repo=CODE,
            number=9,
            head_ref="am/self/docs-9",
            mergeable=Mergeable.CONFLICTING,
        )
    )
    h.service.a.workspace.merge_conflicts = {  # type: ignore[attr-defined]
        "docs/design.md": "<<<<<<< HEAD\nour section\n=======\nmain's section\n>>>>>>> main\n"
    }

    def resolve(task: AgentTask) -> AgentResult:
        assert task.repo == CODE and "docs/design.md" in task.prompt
        (Path(task.workdir) / "docs" / "design.md").write_text(
            "our section\nmain's section\n"
        )
        (Path(task.workdir) / "README.md").write_text("brought by main\n")
        return AgentResult(
            runtime="codex",
            ok=True,
            text="Kept both sections in docs/design.md.",
        )

    h.runner.on("fix", resolve)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, "maria") as maria:
        view = (await maria.get("/api/service")).json()
        assert view["prs"][0]["conflicting"] is True
        refused = await maria.post("/api/service/prs/9/merge")
        assert refused.status_code == 409 and "conflicts with main" in refused.text
        assert h.forge.writes_of("merge_service_pr") == []
        started = await maria.post(
            "/api/service/prs/9/merge-base", json={"agent": "codex"}
        )
        assert started.status_code == 200, started.text
        assert started.json()["run"] == "resolve"
        await h.settle()
        assert any(
            j["playbook"] == "merge_base" for j in (await maria.get("/api/work")).json()
        )
        assert h.service.a.publisher.published == []  # type: ignore[union-attr]
        [proposal] = [
            p for p in (await maria.get("/api/proposals")).json() if p["repo"] == CODE
        ]
        assert proposal["can_decide"] is True
        assert proposal["action"]["action"]["kind"] == "code.update"
        payload = proposal["action"]["action"]["payload"]
        assert payload["runtime"] == "codex"
        assert "docs/design.md" in payload["patch"]
        assert "README.md" not in payload["patch"]

    async with _client(app, "walt") as walt:
        [seen] = [
            p for p in (await walt.get("/api/proposals")).json() if p["repo"] == CODE
        ]
        assert seen["can_decide"] is False
        assert (
            await walt.post(f"/api/proposals/{proposal['id']}/approve", json={})
        ).status_code == 403

    async with _client(app, "maria") as maria:
        approved = await maria.post(f"/api/proposals/{proposal['id']}/approve", json={})
        assert approved.status_code == 200, approved.text
        await h.settle()
        after = (await maria.get("/api/service")).json()

    [pushed] = h.service.a.publisher.published  # type: ignore[union-attr]
    assert pushed["repo"] == CODE and pushed["branch"] == "am/self/docs-9"
    assert pushed["merge_sha"] == "d" * 40
    assert pushed["resolved"] == {"docs/design.md": "our section\nmain's section\n"}
    assert "Requested-by: maria" in pushed["message"]
    assert "Signed-off-by:" not in pushed["message"]
    assert any(
        row["kind"] == "code.update" and row["status"] == "executed"
        for row in after["history"]
    )


def test_only_files_keeps_resolved_files_from_git_patches() -> None:
    from auto_maintainer.playbooks.merge_base import only_files

    docs_block = (
        "diff --git a/docs/design.md b/docs/design.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/docs/design.md\n"
        "+++ b/docs/design.md\n"
        "@@ -1,2 +1,3 @@\n"
        " our section\n"
        "+main's section (mentions diff --git inline)\n"
    )
    readme_block = (
        "diff --git a/README.md b/README.md\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "--- /dev/null\n"
        "+++ b/README.md\n"
        "@@ -0,0 +1 @@\n"
        "+brought by main\n"
    )
    spaced_block = (
        "diff --git a/docs/part b/merge guide.md b/docs/part b/merge guide.md\n"
        "index 4444444..5555555 100644\n"
        "--- a/docs/part b/merge guide.md\t\n"
        "+++ b/docs/part b/merge guide.md\t\n"
        "@@ -1 +1,2 @@\n"
        " kept\n"
        "+resolved\n"
    )
    other_spaced_block = (
        "diff --git a/docs/other notes.md b/docs/other notes.md\n"
        "index 6666666..7777777 100644\n"
        "--- a/docs/other notes.md\t\n"
        "+++ b/docs/other notes.md\t\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+from main\n"
    )
    patch = readme_block + docs_block + other_spaced_block + spaced_block

    assert only_files(patch, {"docs/design.md"}) == docs_block
    assert (
        only_files(patch, {"docs/design.md", "docs/part b/merge guide.md"})
        == docs_block + spaced_block
    )
    assert only_files(patch, set()) == ""
    assert only_files("README.md\nbrought by main\n", {"docs/design.md"}) == ""
    assert only_files("+++ README.md\nbrought by main\n", {"docs/design.md"}) == ""
    assert only_files("", {"docs/design.md"}) == ""
