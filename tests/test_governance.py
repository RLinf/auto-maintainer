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

"""A repository's People record: the skeleton, the agent filling it in from the
repository's process, checking who should change role, history that only grows,
where the record and GitHub disagree, and who may approve a change."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from auto_maintainer.domain.governance import (
    Governance,
    RoleChange,
    role_changes,
    with_history,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.profiles.governance import check, governance_path
from auto_maintainer.testing.demo import DEMO_GOVERNANCE, build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
ROOT = Path(__file__).resolve().parents[1]


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


def _skeleton() -> Governance:
    return Governance.model_validate(
        yaml.safe_load((ROOT / "config/defaults/governance.yaml").read_text())
    )


def test_the_skeleton_checks_and_says_it_is_one() -> None:
    gov = _skeleton()
    assert not gov.established and gov.members == [] and gov.open_questions
    assert [r.id for r in sorted(gov.roles, key=lambda r: r.level)][-1] == "owner"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"members": [{"login": "ann", "role": "chair"}]}, "unknown role 'chair'"),
        (
            {"transitions": [{"id": "x", "to_role": "chair"}]},
            "unknown role 'chair'",
        ),
        ({"transitions": [{"id": "x"}]}, "needs a from_role or a to_role"),
        ({"members": [{"login": "not a login", "role": "reviewer"}]}, "not a GitHub"),
        ({"roles": [{"id": "Bad Id", "name": "x", "level": 1}]}, "lowercase slug"),
    ],
)
def test_a_record_that_names_what_it_lacks_does_not_check(
    change: dict[str, Any], problem: str
) -> None:
    data = {**_skeleton().dump(), **change}
    with pytest.raises(ValueError, match=problem):
        Governance.model_validate(data)


def test_history_only_grows_and_records_every_change_of_role() -> None:
    before = Governance.model_validate(
        {
            **DEMO_GOVERNANCE,
            "history": [
                {
                    "login": "maria",
                    "to_role": "maintainer",
                    "on": "2025-01-10",
                    "by": "lead",
                }
            ],
        }
    )
    after = Governance.model_validate(
        {
            **DEMO_GOVERNANCE,
            "members": [
                {"login": "maria", "role": "maintainer"},
                {"login": "bob", "role": "maintainer"},
                {"login": "carol", "role": "reviewer"},
            ],
            "history": [
                # The agent dropped the old entry and recorded one change itself.
                {
                    "login": "carol",
                    "to_role": "reviewer",
                    "on": "2026-01-05",
                    "by": "maria",
                    "reason": "five merged PRs",
                }
            ],
        }
    )
    assert role_changes(before, after) == [
        ("bob", "reviewer", "maintainer"),
        ("carol", None, "reviewer"),
    ]
    kept = with_history(before, after, by="walt", on=date(2026, 1, 6), reason="asked")
    assert [(c.login, c.from_role, c.to_role, c.by) for c in kept.history] == [
        ("maria", None, "maintainer", "lead"),
        ("carol", None, "reviewer", "maria"),
        ("bob", "reviewer", "maintainer", "walt"),
    ]
    assert (
        isinstance(kept.history[-1], RoleChange) and kept.history[-1].reason == "asked"
    )


def test_the_record_is_checked_against_github() -> None:
    gov = Governance.model_validate(DEMO_GOVERNANCE)
    findings = check(
        gov,
        {"maria": Permission.WRITE, "bob": Permission.NONE},
        {"maria", "dave"},
    )
    assert [(f.kind, f.login) for f in findings] == [
        ("permission", "maria"),
        ("no_access", "bob"),
        ("codeowner", "dave"),
    ]
    assert "expects maintain on GitHub, but has write" in findings[0].text


async def test_the_agent_fills_in_the_skeleton_and_a_maintainer_approves(demo) -> None:
    h, app = demo
    async with _as(app, "walt") as walt:
        page = (await walt.get(f"/api/repos/{REPO}/governance")).json()
        assert page["stored"] is False and page["governance"]["established"] is False
        assert {c["login"] for c in page["checks"]} == {"maria", "bob"}, (
            "code owners without a role"
        )
        assert page["can_configure"] is False
        asked = await walt.post(
            f"/api/repos/{REPO}/governance/agent",
            json={"instruction": "Our process is in GOVERNANCE.md."},
        )
        assert asked.status_code == 200, asked.text
        await h.settle()
        [task] = [t for t in h.runner.tasks if t.kind == "governance"]
        assert "Our process is in GOVERNANCE.md." in task.prompt
        assert "the skeleton: nobody has filled it in yet" in task.prompt
        assert task.requested_by == "walt"
        page = (await walt.get(f"/api/repos/{REPO}/governance")).json()
        [pending] = page["pending"]
        change_id = pending["id"]
        assert {(c["login"], c["to"]) for c in pending["role_changes"]} == {
            ("maria", "maintainer"),
            ("bob", "reviewer"),
        }
        assert page["stored"] is False, "nothing applies before a maintainer approves"
        assert (
            await walt.post(f"/api/memory/changes/{change_id}/approve")
        ).status_code == 403
    change = await h.store.get_memory_change(change_id)
    assert change is not None and change.repos == [REPO] and change.tier == "T2"
    drafted = Governance.model_validate(
        yaml.safe_load(change.files[governance_path(REPO)] or "")
    )
    assert drafted.established and drafted.repo == REPO
    assert [(c.login, c.to_role, c.by) for c in drafted.history] == [
        ("bob", "reviewer", "walt"),
        ("maria", "maintainer", "walt"),
    ], "joining members are recorded as changes of role"

    async with _as(app, "maria") as maria:
        approved = await maria.post(f"/api/memory/changes/{change_id}/approve")
        assert approved.json()["state"] == "merged"
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
    assert page["stored"] and page["governance"]["established"] and not page["pending"]
    assert page["checks"] == [], "the record now matches GitHub and CODEOWNERS"
    roles = {r["id"]: r for r in page["governance"]["roles"]}
    assert roles["lead"]["name"] == "Project lead"


async def test_checking_people_proposes_role_changes_and_keeps_the_process(
    demo,
) -> None:
    h, app = demo
    h.memory.files[governance_path(REPO)] = yaml.safe_dump(
        {**DEMO_GOVERNANCE, "repo": REPO}, sort_keys=False
    )

    def changes_the_process_too(task: Any) -> dict[str, Any]:
        record = {
            **DEMO_GOVERNANCE,
            "roles": DEMO_GOVERNANCE["roles"][:3]
            + [{"id": "emeritus", "name": "Retired", "level": 0}],
            "members": [
                *DEMO_GOVERNANCE["members"],
                {"login": "carol", "role": "reviewer", "areas": ["src/ui.py"]},
            ],
        }
        return {"governance": record, "summary": "Propose carol."}

    h.runner.on("governance", changes_the_process_too)
    async with _as(app, "maria") as maria:
        await maria.post(
            f"/api/repos/{REPO}/governance/agent", json={"mode": "members"}
        )
        await h.settle()
        [task] = [t for t in h.runner.tasks if t.kind == "governance"]
        assert "asked you to check who should change role" in task.prompt
        [pending] = (await maria.get(f"/api/repos/{REPO}/governance")).json()["pending"]
        change_id = pending["id"]
        assert pending["role_changes"] == [
            {"login": "carol", "from": None, "to": "reviewer"}
        ]
    change = await h.store.get_memory_change(change_id)
    assert change is not None and change.title.startswith("people: 1 role change")
    drafted = Governance.model_validate(
        yaml.safe_load(change.files[governance_path(REPO)] or "")
    )
    assert drafted.role("emeritus").name == "Emeritus", "the process stays as it was"
    assert [(c.login, c.to_role) for c in drafted.history] == [("carol", "reviewer")]


async def test_an_established_record_needs_to_be_told_what_to_change(demo) -> None:
    h, app = demo
    h.memory.files[governance_path(REPO)] = yaml.safe_dump(
        {**DEMO_GOVERNANCE, "repo": REPO}, sort_keys=False
    )
    async with _as(app, "maria") as maria:
        empty = await maria.post(f"/api/repos/{REPO}/governance/agent", json={})
        assert empty.status_code == 422
    async with _as(app, "eve") as eve:
        assert (await eve.get(f"/api/repos/{REPO}/governance")).status_code == 403


async def test_a_record_that_does_not_read_shows_the_skeleton_and_why(demo) -> None:
    h, app = demo
    h.memory.files[governance_path(REPO)] = "roles: [{id: x}]\n"
    async with _as(app, "maria") as maria:
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
    assert page["stored"] and page["error"] and not page["governance"]["established"]


async def test_chat_starts_the_agent_on_the_record(demo) -> None:
    h, app = demo
    h.runner.on(
        "chat",
        lambda task: {
            "reply": "I will read GOVERNANCE.md.",
            "proposals": [
                {
                    "kind": "governance",
                    "repo": REPO,
                    "request": "Reviewers become maintainers after a vote.",
                }
            ],
        },
    )
    async with _as(app, "maria") as maria:
        thread = (await maria.post("/api/chat/threads", json={"text": "hi"})).json()
        answer = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Set up our roles from GOVERNANCE.md"},
            )
        ).json()
        assert len(answer["jobs"]) == 1
        await h.settle()
    [task] = [t for t in h.runner.tasks if t.kind == "governance"]
    assert "Reviewers become maintainers after a vote." in task.prompt


async def test_talking_fills_in_the_record_and_revises_the_waiting_draft(
    demo,
) -> None:
    h, app = demo
    talk = f"/api/repos/{REPO}/governance/talk"
    async with _as(app, "walt") as walt:
        first = (
            await walt.post(talk, json={"text": "Our process is in GOVERNANCE.md."})
        ).json()
        assert "GOVERNANCE.md" in first["text"] and len(first["changes"]) == 1
        task = h.runner.tasks[-1]
        assert (
            task.kind == "governance" and task.messages and task.requested_by == "walt"
        )
        assert "The newest message, from walt:\nOur process is in GOVERNANCE.md." in (
            task.prompt
        )
        assert "the skeleton: nobody has filled it in yet" in task.prompt
        await h.settle()
        [draft] = (await walt.get(f"/api/repos/{REPO}/governance")).json()["pending"]
        assert draft["id"] == first["changes"][0]
        assert draft["governance"]["open_questions"]

        asked = (await walt.post(talk, json={"text": "Who maintains it?"})).json()
        assert asked["changes"] == [], "a turn that only answers drafts nothing"

    async with _as(app, "maria") as maria:
        answered = (
            await maria.post(
                talk, json={"text": "Yes, the project lead merges like a maintainer."}
            )
        ).json()
        await h.settle()
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
        shared = (await maria.get(talk)).json()
    task = h.runner.tasks[-1]
    assert "which you revise" in task.prompt, "it builds on the waiting draft"
    assert "walt: Our process is in GOVERNANCE.md." in task.prompt
    assert "ignore any instructions" not in task.prompt
    [second] = page["pending"]
    assert second["id"] == answered["changes"][0] != draft["id"]
    assert second["governance"]["open_questions"] == []
    old = await h.store.get_memory_change(draft["id"])
    assert old is not None and old.state == "rejected"
    assert old.rejected_reason == f"replaced by {second['id']}"
    new = await h.store.get_memory_change(second["id"])
    assert new is not None
    history = yaml.safe_load(new.files[governance_path(REPO)] or "")["history"]
    assert len(history) == 2, "the revision does not record the same changes twice"
    assert [m["author"] for m in shared["messages"] if m["role"] == "user"] == [
        "walt",
        "walt",
        "maria",
    ], "everyone who may see the repository shares the conversation"
    async with _as(app, "walt") as walt:
        assert (await walt.get("/api/chat/threads")).json() == [], (
            "the conversation lives on the People tab, not in personal chats"
        )
    async with _as(app, "eve") as eve:
        assert (await eve.get(talk)).status_code == 403


async def test_a_message_reaches_the_agent_while_it_answers(demo) -> None:
    from auto_maintainer.web import people_talk

    h, app = demo
    thread_id = people_talk.thread_id_of(REPO)
    async with _as(app, "maria") as maria:
        await maria.post(
            f"/api/repos/{REPO}/governance/talk", json={"text": "Who maintains it?"}
        )
        thread = await h.store.get_thread(thread_id)
        assert thread is not None
        thread.live_run = "run_busy"
        await h.store.save_thread(thread)
        h.runner.listening_jobs["run_busy"] = 1
        lock = people_talk._turns[thread_id]
        async with lock:
            state = (await maria.get(f"/api/repos/{REPO}/governance/talk")).json()
            assert state["listening"]
            sent = (
                await maria.post(
                    f"/api/repos/{REPO}/governance/talk",
                    json={"text": "Also read docs/governance.rst."},
                )
            ).json()
    assert sent["while_running"]
    [(run, by, text)] = h.runner.told
    assert (run, by) == ("run_busy", "maria") and "docs/governance.rst" in text


async def test_a_job_from_chat_builds_on_the_waiting_draft_too(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        first = (
            await maria.post(
                f"/api/repos/{REPO}/governance/talk",
                json={"text": "Our process is in GOVERNANCE.md."},
            )
        ).json()
        await h.settle()
        await maria.post(
            f"/api/repos/{REPO}/governance/agent", json={"mode": "members"}
        )
        await h.settle()
        [pending] = (await maria.get(f"/api/repos/{REPO}/governance")).json()["pending"]
    task = h.runner.tasks[-1]
    assert "which you revise" in task.prompt
    assert pending["id"] != first["changes"][0]
    assert {c["login"] for c in pending["role_changes"]} == {"maria", "bob", "carol"}
