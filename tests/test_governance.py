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
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.profiles.governance import (
    GovernanceRecords,
    check,
    governance_path,
)
from auto_maintainer.testing.demo import DEMO_GOVERNANCE, build_demo
from auto_maintainer.testing.fakes import FakeMemory
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
        sent = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Set up our roles from GOVERNANCE.md"},
            )
        ).json()
        await app.state.live_turns.wait(sent["run"], timeout=5)
        answer = (await maria.get(f"/api/chat/threads/{thread['id']}/messages")).json()[
            -1
        ]
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
        assert (
            "The newest message, from walt, which you answer:\nOur process is in GOVERNANCE.md."
            in (task.prompt)
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


async def test_the_record_read_is_always_the_stored_one() -> None:
    """The gate reads the record on every check, so its parse is kept while the text
    stays the same; a change to the text is read at once, and what one caller does
    with what it loaded never reaches the next."""
    path = governance_path("acme/widget")
    memory = FakeMemory({path: yaml.safe_dump(DEMO_GOVERNANCE)})
    records = GovernanceRecords(memory)
    first = await records.load("acme/widget")
    assert first.error is None and "maria" in first.governance.maintainers()
    first.governance.members.clear()
    assert "maria" in await records.maintainers_of("acme/widget")

    changed = {
        **DEMO_GOVERNANCE,
        "members": [
            m for m in DEMO_GOVERNANCE["members"] if m["login"].lower() != "maria"
        ],
    }
    memory.files[path] = yaml.safe_dump(changed)
    assert "maria" not in await records.maintainers_of("acme/widget")
    memory.files[path] = "members: [unclosed"
    broken = await records.load("acme/widget")
    assert broken.error and broken.stored


async def test_people_with_access_on_github_show_with_a_role_to_add_them_as(
    demo,
) -> None:
    """The People tab lists everyone GitHub gives access to the repository, each
    with the role their permission suggests; who has signed in is shown only to a
    maintainer."""
    h, app = demo
    h.forge.set_permission(REPO, "tara", Permission.TRIAGE)
    async with _as(app, "walt") as walt:
        page = (await walt.get(f"/api/repos/{REPO}/governance")).json()
    on_github = {p["login"]: p for p in page["github"]}
    assert on_github["walt"] == {
        "login": "walt",
        "permission": "write",
        "role": "reviewer",
    }
    assert on_github["maria"]["role"] == "owner", "an admin, as the skeleton's owner"
    assert on_github["tara"]["role"] is None, "no role expects triage"
    assert page["signed_in"] == []
    await h.service.a.user_tokens.set("nina", UserToken(token="t"))
    async with _as(app, "maria") as maria:
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
    assert page["signed_in"] == ["nina"]


async def test_a_maintainer_adds_people_at_once_and_a_reviewer_proposes_them(
    demo,
) -> None:
    h, app = demo
    url = f"/api/repos/{REPO}/governance/members"
    async with _as(app, "walt") as walt:
        out = await walt.post(
            url, json={"people": [{"login": "@walt", "role": "reviewer"}]}
        )
        assert out.status_code == 200, out.text
        assert out.json()["applied"] is False, "a reviewer's addition waits"
        page = (await walt.get(f"/api/repos/{REPO}/governance")).json()
        assert page["stored"] is False and len(page["pending"]) == 1
        refused = await walt.post(
            url, json={"people": [{"login": "bob", "role": "chief"}]}
        )
        assert refused.status_code == 422 and "no role" in refused.text
    async with _as(app, "maria") as maria:
        out = await maria.post(
            url,
            json={
                "people": [
                    {"login": "maria", "role": "owner"},
                    {"login": "bob", "role": "reviewer", "note": "web UI"},
                ]
            },
        )
        assert out.status_code == 200, out.text
        assert out.json()["on_draft"] is True, "it builds on walt's waiting draft"
        assert out.json()["applied"] is False, "which nobody approved yet"
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
        [pending] = page["pending"]
        assert {(c["login"], c["to"]) for c in pending["role_changes"]} == {
            ("walt", "reviewer"),
            ("maria", "owner"),
            ("bob", "reviewer"),
        }
        approved = await maria.post(f"/api/memory/changes/{pending['id']}/approve")
        assert approved.json()["state"] == "merged"
        listed = await maria.post(
            url, json={"people": [{"login": "Bob", "role": "owner"}]}
        )
        assert listed.status_code == 422 and "listed already" in listed.text

        h.forge.set_permission(REPO, "nina", Permission.WRITE)
        out = await maria.post(
            url, json={"people": [{"login": "nina", "role": "reviewer"}]}
        )
        assert out.json()["applied"] is True, "a maintainer's own addition applies"
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
    members = {(m["login"], m["role"]) for m in page["governance"]["members"]}
    assert members == {
        ("walt", "reviewer"),
        ("maria", "owner"),
        ("bob", "reviewer"),
        ("nina", "reviewer"),
    }
    history = page["governance"]["history"]
    assert {(c["login"], c["to_role"], c["by"]) for c in history} >= {
        ("nina", "reviewer", "maria"),
        ("walt", "reviewer", "walt"),
    }, "every addition is recorded"
    assert not page["pending"]


async def test_a_maintainer_changes_and_removes_members_directly_and_a_reviewer_proposes(
    demo,
) -> None:
    h, app = demo
    base = f"/api/repos/{REPO}/governance/members"
    async with _as(app, "maria") as maria:
        added = await maria.post(
            base,
            json={
                "people": [
                    {"login": "maria", "role": "owner"},
                    {"login": "bob", "role": "reviewer"},
                    {"login": "walt", "role": "reviewer"},
                ]
            },
        )
        assert added.status_code == 200 and added.json()["applied"] is True

        # Maintainer promotes bob directly: applied at once and recorded in history.
        promoted = await maria.patch(
            f"{base}/bob",
            json={"role": "maintainer", "reason": "promoted after Q4 reviews"},
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["applied"] is True
        assert promoted.json()["role"] == "maintainer"
        assert "bob" in await h.service.governance.maintainers_of(REPO)

        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
        bob_entry = next(
            m for m in page["governance"]["members"] if m["login"] == "bob"
        )
        assert bob_entry["role"] == "maintainer"
        assert any(
            c.get("login") == "bob"
            and c.get("from_role") == "reviewer"
            and c.get("to_role") == "maintainer"
            and c.get("by") == "maria"
            and c.get("reason") == "promoted after Q4 reviews"
            for c in page["governance"]["history"]
        )

    # A non-maintainer's direct role change becomes a pending draft for maintainers.
    async with _as(app, "walt") as walt:
        proposed = await walt.patch(
            f"{base}/walt", json={"role": "maintainer", "reason": "nominating myself"}
        )
        assert proposed.status_code == 200 and proposed.json()["applied"] is False
        assert "walt" not in await h.service.governance.maintainers_of(REPO)

    # Maintainer rejects walt's draft, then removes bob directly.
    async with _as(app, "maria") as maria:
        pending = (await maria.get(f"/api/repos/{REPO}/governance")).json()["pending"]
        assert len(pending) == 1
        rejected = await maria.post(
            f"/api/memory/changes/{pending[0]['id']}/reject",
            json={"reason": "not ready yet"},
        )
        assert rejected.status_code == 200, rejected.text

        removed = await maria.delete(f"{base}/bob?reason=stepped+down")
        assert removed.status_code == 200, removed.text
        assert removed.json()["applied"] is True
        assert "bob" not in await h.service.governance.maintainers_of(REPO)
        page = (await maria.get(f"/api/repos/{REPO}/governance")).json()
        assert all(m["login"] != "bob" for m in page["governance"]["members"])
        assert any(
            c.get("login") == "bob"
            and c.get("from_role") == "maintainer"
            and c.get("to_role") is None
            and c.get("reason") == "stepped down"
            for c in page["governance"]["history"]
        )


async def test_reject_endpoints_allow_empty_or_omitted_reason(demo) -> None:
    """Both proposal and memory-change reject endpoints accept an empty or omitted
    reason."""
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post(
            f"/api/prs/{REPO}/42/review",
            json={},
        )
        await h.settle()
        proposals = (await maria.get("/api/proposals")).json()
        assert proposals
        res_prop = await maria.post(
            f"/api/proposals/{proposals[0]['id']}/reject", json={}
        )
        assert res_prop.status_code == 200, res_prop.text
        assert res_prop.json()["state"] == "rejected"
        assert res_prop.json()["reason"] is None

    from auto_maintainer.domain.memory import MemoryChange, Tier

    change = MemoryChange(
        tier=Tier.T1,
        title="draft note",
        files={f"repos/{REPO}/notes.md": "hello\n"},
        repos=[REPO],
        requested_by="walt",
    )
    await h.store.save_memory_change(change)

    async with _as(app, "maria") as maria:
        res_mem = await maria.post(
            f"/api/memory/changes/{change.id}/reject", json={"reason": ""}
        )
        assert res_mem.status_code == 200, res_mem.text
        assert res_mem.json()["state"] == "rejected"
        assert res_mem.json()["rejected_reason"] == "rejected by maria"
