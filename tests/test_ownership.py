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

"""The service's repository and its one owner, on the Settings page (design 2.3):
taking ownership on a host set up before owners, handing it to another signed-in
admin, and moving to another fork that passes the checks."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.config import HostConfig, load_host_config, save_host_config
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.hostsetup import ForgeSteps
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/auto-maintainer"
FORK = "maria/auto-maintainer"


@pytest.fixture
async def site(tmp_path: Path):
    h = await build_demo(tmp_path)
    config = tmp_path / "config"
    save_host_config(
        HostConfig(code_repo=REPO, owner="maria", data_dir=str(tmp_path / "data")),
        config,
    )
    for login, role in (("ada", Permission.ADMIN), ("bob", Permission.WRITE)):
        h.forge.set_permission(REPO, login, role)
    steps = ForgeSteps(config, tmp_path / "data")
    results: dict[str, list[dict[str, Any]]] = {}

    async def check_repo(
        forge: Any, path: str
    ) -> tuple[list[dict[str, Any]], str, str]:
        return results.get(path, []), path, "trunk"

    steps.check_repo = check_repo  # type: ignore[method-assign]
    started: list[HostConfig] = []

    async def on_change(cfg: HostConfig) -> None:
        started.append(cfg)

    app = create_app(
        h.service,
        data_dir=tmp_path,
        allow_dev_login=True,
        setup_steps=lambda: None,
        on_host_change=on_change,
        forge_steps=steps,
    )
    yield h, app, config, results, started
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_on_a_host_set_up_before_owners_an_admin_takes_ownership(site) -> None:
    h, app, config, _, _ = site
    h.service.settings.owner = None
    async with _as(app, "bob") as bob:
        view = (await bob.get("/api/settings/owner")).json()
        assert view["may_take"] is False and "admin" in view["reason"]
        assert (await bob.post("/api/settings/owner/take")).status_code == 403
    async with _as(app, "ada") as ada:
        assert (await ada.get("/api/settings/owner")).json()["may_take"] is True
        assert (await ada.post("/api/settings/owner/take")).json() == {"owner": "ada"}
        assert (await ada.post("/api/settings/owner/take")).status_code == 409
    assert h.service.settings.owner == "ada"
    assert load_host_config(config).owner == "ada"


async def test_the_owner_hands_ownership_to_a_signed_in_admin(site) -> None:
    h, app, config, _, _ = site
    tokens = h.service.a.user_tokens
    async with _as(app, "ada") as ada:
        refused = await ada.post("/api/settings/owner/transfer", json={"login": "ada"})
        assert refused.status_code == 403 and "only maria" in refused.text
    async with _as(app, "maria") as maria:
        view = (await maria.get("/api/settings/owner")).json()
        assert view["is_owner"] is True and view["owner"] == "maria"
        absent = await maria.post("/api/settings/owner/transfer", json={"login": "ada"})
        assert absent.status_code == 422 and "has not signed in" in absent.text
        await tokens.set("bob", UserToken(token="b"))
        writer = await maria.post("/api/settings/owner/transfer", json={"login": "bob"})
        assert writer.status_code == 422 and "must be an admin" in writer.text
        await tokens.set("ada", UserToken(token="a"))
        done = await maria.post("/api/settings/owner/transfer", json={"login": "ada"})
        assert done.json() == {"owner": "ada"}
        assert (await maria.get("/api/settings/owner")).json()["is_owner"] is False
    assert load_host_config(config).owner == "ada"
    assert (await h.service.service_gate("maria")).allowed is False


async def test_the_owner_moves_the_service_to_a_fork_that_passes_its_checks(
    site,
) -> None:
    h, app, config, results, started = site
    h.forge.set_permission(FORK, "maria", Permission.ADMIN)
    await h.service.a.user_tokens.set("maria", UserToken(token="m"))
    link = f"https://github.com/{FORK}"
    results[FORK] = [
        {"id": "code", "ok": True, "text": "it holds the code"},
        {"id": "push", "ok": False, "text": "the service's account cannot push to it"},
    ]
    async with _as(app, "maria") as maria:
        elsewhere = await maria.post(
            "/api/settings/owner/repo", json={"link": "https://git.corp/x/am"}
        )
        assert elsewhere.status_code == 422 and "add it under Forges" in elsewhere.text
        failed = await maria.post("/api/settings/owner/repo", json={"link": link})
        assert failed.status_code == 422 and "cannot push" in failed.text
        assert started == []
        results[FORK][1]["ok"] = True
        moved = await maria.post("/api/settings/owner/repo", json={"link": link})
        assert moved.status_code == 200, moved.text
    [cfg] = started
    assert (cfg.code_repo, cfg.owner, cfg.self_update.branch) == (
        FORK,
        "maria",
        "trunk",
    )
    assert load_host_config(config).code_repo == FORK


async def test_a_repository_is_checked_with_the_services_own_account(
    tmp_path: Path, monkeypatch
) -> None:
    import dataclasses
    import subprocess

    from auto_maintainer.forges import registry
    from auto_maintainer.forges.base import ForgeAccount, RepoAccess

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
    ):
        subprocess.run(["git", *args], cwd=checkout, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
    ).stdout.strip()
    seen: list[str] = []

    async def repo(api: str, token: str, path: str) -> RepoAccess | None:
        seen.append(token)
        role = Permission.WRITE if path == "maria/am" else Permission.READ
        return RepoAccess(path, False, role, "trunk", "")

    async def has_commit(api: str, token: str, path: str, sha: str) -> bool:
        return sha == head

    async def unused(*args: Any) -> Any:
        raise AssertionError

    monkeypatch.setitem(
        registry.KINDS,
        "github",
        dataclasses.replace(
            registry.KINDS["github"],
            account=ForgeAccount(repo, has_commit, unused, unused),
        ),
    )
    save_host_config(HostConfig(), tmp_path / "config")
    steps = ForgeSteps(tmp_path / "config", tmp_path / "data", checkout)

    async def service_token(forge: Any) -> str:
        return "service-token"

    monkeypatch.setattr(steps, "_service_token", service_token)
    forge, name = steps.locate("git@github.com:maria/am.git")
    assert name == "maria/am"
    checks, path, branch = await steps.check_repo(forge, name)
    assert [c["ok"] for c in checks] == [True, True] and branch == "trunk"
    checks, _, _ = await steps.check_repo(forge, "other/am")
    assert [c["id"] for c in checks if not c["ok"]] == ["push"]
    assert set(seen) == {"service-token"}
