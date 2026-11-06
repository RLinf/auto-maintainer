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

"""The dashboard API: sign-in, view permissions, the configuration gate, proposals, overrides."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from auto_maintainer.domain.memory import lock_path
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.profiles.layers import dump_yaml
from auto_maintainer.profiles.loader import IN_REPO_PATH
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _client(app, login: str | None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        if login:
            await client.get(f"/auth/dev?login={login}")
            client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_sign_in_and_view_permissions(demo) -> None:
    h, app = demo
    async with _client(app, None) as anon:
        assert (await anon.get("/api/prs")).status_code == 401
    async with _client(app, "stranger") as stranger:
        assert (await stranger.get("/api/prs")).json() == []
        assert (await stranger.get("/api/prs/acme/widget/42")).status_code == 403
    async with _client(app, "walt") as walt:
        rows = (await walt.get("/api/prs")).json()
        assert {r["number"] for r in rows} == {42, 43, 44}
        assert (
            await walt.post("/api/pause", json={}, headers={"x-am-csrf": "wrong"})
        ).status_code == 403


async def test_settings_change_needs_a_maintainer(demo) -> None:
    h, app = demo
    yaml_text = "schema: 1\nextends: [dco]\nmerge: {enabled: false}\n"
    async with _client(app, "walt") as walt:
        profile = (await walt.get("/api/repos/acme/widget/profile")).json()
        assert (
            profile["can_configure"] is False
            and "needs maintain" in profile["gate"]["reason"]
        )
        out = (
            await walt.put("/api/repos/acme/widget/settings", json={"yaml": yaml_text})
        ).json()
        assert out["approved"] is False and out["change"]["state"] == "open"
        change_id = out["change"]["id"]
        assert (
            await walt.post(f"/api/memory/changes/{change_id}/approve")
        ).status_code == 403
    assert (await h.service.profiles.active("acme/widget")).merge.enabled is True
    async with _client(app, "maria") as maria:
        out = (await maria.post(f"/api/memory/changes/{change_id}/approve")).json()
        assert out["state"] == "merged"
    assert (await h.service.profiles.active("acme/widget")).merge.enabled is False


async def test_profile_approval_reports_a_failed_memory_write(demo) -> None:
    h, app = demo
    h.forge.put_file(
        "acme/widget", IN_REPO_PATH, dump_yaml({"schema": 1, "scope": {"min_pr": 9}})
    )
    state = await h.service.profiles.refresh("acme/widget")
    assert state.candidate_hash
    stage = h.memory.stage_change
    calls = 0

    async def push_rejected_once(change):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("git exited with 1: failed to push some refs")
        return await stage(change)

    h.memory.stage_change = push_rejected_once
    url = "/api/repos/acme/widget/profile/approve"
    async with _client(app, "maria") as maria:
        failed = await maria.post(url, json={"hash": state.candidate_hash})
        assert failed.status_code == 502
        assert "failed to push" in failed.json()["detail"]
        assert (await h.service.profiles.active("acme/widget")).scope.min_pr != 9

        retried = await maria.post(url, json={"hash": state.candidate_hash})
        assert retried.status_code == 200, retried.text
        assert retried.json()["change"]["state"] == "merged"
    locks = [
        c
        for c in await h.store.list_memory_changes(None)
        if c.repos == ["acme/widget"] and lock_path("acme/widget") in c.files
    ]
    assert len(locks) == 1, "the retry approves the change it already staged"
    assert (await h.service.profiles.active("acme/widget")).scope.min_pr == 9


async def test_a_settings_change_lists_its_profile_before_and_after(demo) -> None:
    h, app = demo
    yaml_text = "schema: 1\nextends: [dco]\nmerge: {enabled: false}\n"
    async with _client(app, "walt") as walt:
        out = (
            await walt.put("/api/repos/acme/widget/settings", json={"yaml": yaml_text})
        ).json()
        assert out["before"]["merge"]["enabled"] is True
        assert out["after"]["merge"]["enabled"] is False
        listed = (await walt.get("/api/memory/changes")).json()
    change = next(c for c in listed if c["id"] == out["change"]["id"])
    [profile] = change["profiles"]
    assert profile["repo"] == "acme/widget"
    assert profile["before"]["merge"]["enabled"] is True
    assert profile["after"]["merge"]["enabled"] is False


async def test_saving_settings_reports_a_failed_memory_write(demo) -> None:
    h, app = demo

    async def push_rejected(change):
        raise RuntimeError("git exited with 1: failed to push some refs")

    h.memory.stage_change = push_rejected
    async with _client(app, "maria") as maria:
        out = await maria.put(
            "/api/repos/acme/widget/settings",
            json={"yaml": "schema: 1\nmerge: {enabled: false}\n"},
        )
    assert out.status_code == 502 and "failed to push" in out.json()["detail"]
    assert (await h.service.profiles.active("acme/widget")).merge.enabled is True


async def test_approve_and_edit_a_proposal(demo) -> None:
    h, app = demo
    async with _client(app, "maria") as maria:
        targets = [{"repo": "acme/widget", "number": n} for n in (42, 43)]
        started = (await maria.post("/api/reviews", json={"prs": targets})).json()
        assert [r["run"] for r in started] == ["review", "review"]
        await h.settle()
        rows = {r["number"]: r for r in (await maria.get("/api/prs")).json()}
        assert rows[42]["review_activity"]["draft"]["for"] == "maria"
        proposals = (await maria.get("/api/proposals")).json()
        review = next(
            p
            for p in proposals
            if p["action"]["action"]["kind"] == "review.post"
            and p["action"]["action"]["number"] == 42
        )
        payload = dict(review["action"]["action"]["payload"])
        payload["body"] = payload["body"] + "\n\nThanks for the fix."
        out = await maria.post(
            f"/api/proposals/{review['id']}/approve", json={"payload": payload}
        )
        assert out.status_code == 200, out.text
        await h.settle()
        [posted] = h.forge.writes_of("create_review")
        assert posted["body"].rstrip().endswith("approved by maria.</sub>")
        assert "Thanks for the fix." in posted["body"]
        signals = await h.store.list_signals("acme/widget")
        assert any(s.kind == "draft_edited" for s in signals), "the edit is a signal"

        h.service.set_dev_mode(True)
        pending = (await maria.get("/api/proposals")).json()
        other = next(
            p for p in pending if p["action"]["action"]["kind"] == "review.post"
        )
        approve = await maria.post(f"/api/proposals/{other['id']}/approve", json={})
        assert approve.status_code == 200
        await h.settle()
        assert len(h.forge.writes_of("create_review")) == 1, "dev mode holds approvals"


async def test_override_requires_reason_and_starts_calibration(demo) -> None:
    h, app = demo
    async with _client(app, "walt") as walt:
        assert (
            await walt.post(
                "/api/prs/acme/widget/43/override", json={"tier": "P0", "reason": " "}
            )
        ).status_code == 422
        out = (
            await walt.post(
                "/api/prs/acme/widget/43/override",
                json={"tier": "P0", "reason": "partner deadline this week"},
            )
        ).json()
        assert out["tier"] == "P0" and out["override_by"] == "walt"
        await h.settle()
    threads = await h.store.list_threads("walt")
    assert threads and "Calibration" in threads[0].title


async def test_an_unbuilt_dashboard_explains_itself(
    demo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auto_maintainer.web.app as web_app
    from auto_maintainer.web.dashboard import ensure_built

    h, _ = demo
    monkeypatch.setattr(web_app, "STATIC", tmp_path / "no-static")
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with _client(app, None) as anon:
        page = await anon.get("/")
        assert page.status_code == 503 and "The dashboard is not built" in page.text
        assert "npm ci" in page.text
        assert (await anon.get("/queue/anything")).status_code == 503
        assert (await anon.get("/api/no-such-endpoint")).status_code == 404
        assert (await anon.get("/healthz")).status_code == 200
    messages: list[str] = []
    assert not ensure_built(tmp_path / "no-ui", tmp_path / "no-static", messages.append)
    assert "no sources" in messages[0]


def test_the_dashboard_rebuilds_when_its_sources_change(tmp_path: Path) -> None:
    import os

    from auto_maintainer.web.dashboard import needs_build

    ui, static = tmp_path / "ui", tmp_path / "static"
    (ui / "src").mkdir(parents=True)
    (ui / "package.json").write_text("{}")
    (ui / "src" / "App.tsx").write_text("old")
    assert needs_build(ui, static)
    static.mkdir()
    (static / "index.html").write_text("<html>")
    assert not needs_build(ui, static)
    later = (static / "index.html").stat().st_mtime + 10
    os.utime(ui / "src" / "App.tsx", (later, later))
    assert needs_build(ui, static)


async def test_the_agent_knows_the_page_it_is_asked_from(demo) -> None:
    h, app = demo
    seen: list[str] = []

    def answer(task):
        seen.append(task.prompt)
        return {"reply": "It needs one more approval.", "proposals": []}

    h.runner.on("chat", answer)
    context = {"page": "pr", "repo": "acme/widget", "number": 42}
    async with _client(app, "walt") as walt:
        thread = (
            await walt.post(
                "/api/chat/threads", json={"text": "status?", "context": context}
            )
        ).json()
        assert thread["repo"] == "acme/widget"
        reply = await walt.post(
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "What blocks this PR?", "context": context},
        )
        assert reply.status_code == 200, reply.text
        hidden = await walt.post(
            "/api/chat/threads",
            json={"text": "x", "context": {"page": "repo", "repo": "other/secret"}},
        )
        assert hidden.status_code == 403
    assert "looking at acme/widget#42" in seen[0] and "'this PR'" in seen[0]
