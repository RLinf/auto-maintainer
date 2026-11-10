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

"""Adding a forge from the dashboard (design 5.16): only maintainers of the service's
repository may, the service signs in there into its own folder, and the forge is kept
only once the service has started with it."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer import hostsetup
from auto_maintainer.config import HostConfig, load_host_config, save_host_config
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.forges import registry
from auto_maintainer.hostsetup import ForgeSteps
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

PAT = "glpat-" + "x" * 20


class FakeLogin:
    """The forge CLI's sign-in: ``with_token`` at once, or a one-time code that
    the test then approves."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.signed_in: str | None = None
        self.started = False
        self.discarded = False

    async def with_token(self, token: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.signed_in = token.strip()

    async def start(self) -> dict[str, str]:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.started = True
        return {"code": "WXYZ-1234", "url": "https://gitlab.example.com/oauth/device"}

    def approve(self) -> None:
        self.signed_in = PAT

    def state(self) -> str:
        return "done" if self.signed_in else "pending"

    async def token(self) -> str | None:
        return self.signed_in

    def discard(self) -> None:
        self.discarded = True

    def cancel(self) -> None:
        pass


@pytest.fixture
async def site(tmp_path: Path, monkeypatch):
    """The demo service with host setup on, a host config of one GitHub forge, and
    the GitLab CLI's sign-in and account lookup faked."""
    folder = tmp_path / "config"
    folder.mkdir()
    save_host_config(HostConfig(data_dir=str(tmp_path / "data")), folder)
    logins: list[FakeLogin] = []

    def cli_login(kind: str, config_dir: Path, web_url: str, **kw: Any) -> FakeLogin:
        logins.append(FakeLogin(config_dir))
        return logins[-1]

    async def who(api_url: str, token: str) -> dict[str, str]:
        if token != PAT:
            raise RuntimeError("401")
        return {"login": "am-bot", "name": "am bot", "avatar_url": ""}

    monkeypatch.setattr(hostsetup, "cli_login", cli_login)
    monkeypatch.setitem(
        registry.KINDS, "gitlab", dataclasses.replace(registry.KINDS["gitlab"], who=who)
    )
    monkeypatch.setattr(hostsetup.shutil, "which", lambda name: f"/bin/{name}")
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    started: list[HostConfig] = []
    fail: list[Exception] = []

    async def on_change(cfg: HostConfig) -> None:
        if fail:
            raise fail[0]
        started.append(cfg)

    app = create_app(
        h.service,
        data_dir=tmp_path,
        allow_dev_login=True,
        setup_steps=lambda: None,
        on_host_change=on_change,
        forge_steps=ForgeSteps(folder, tmp_path / "data"),
    )
    yield app, logins, started, fail
    await h.close()


@contextlib.asynccontextmanager
async def _signed_in(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


CORP = {"id": "corp", "kind": "gitlab", "web_url": "https://gitlab.example.com/"}


async def test_only_maintainers_of_the_service_repo_add_a_forge(
    site, tmp_path: Path
) -> None:
    app, logins, started, _ = site
    async with _signed_in(app, "walt") as walt:
        listed = await walt.get("/api/settings/forges")
        assert [f["id"] for f in listed.json()["forges"]] == ["github"]
        refused = await walt.post("/api/settings/forges", json={**CORP, "token": PAT})
        assert refused.status_code == 403
    assert logins == [] and started == []
    assert [f.id for f in load_host_config(tmp_path / "config").forges] == ["github"]


async def test_a_forge_signed_in_with_a_token_is_kept_once_the_service_runs_it(
    site, tmp_path: Path
) -> None:
    app, logins, started, _ = site
    async with _signed_in(app, "maria") as maria:
        done = await maria.post("/api/settings/forges", json={**CORP, "token": PAT})
        assert done.status_code == 200
        assert done.json()["forges"] == [{"added": "corp", "signed_in_as": "am-bot"}]
        again = await maria.post("/api/settings/forges", json={**CORP, "token": PAT})
        assert again.status_code == 409
    kept = load_host_config(tmp_path / "config").forge("corp")
    assert kept.web == "https://gitlab.example.com"
    assert kept.api == "https://gitlab.example.com/api/v4"
    # The service's own folder for that CLI, never the host user's.
    assert kept.cli_config_dir == str((tmp_path / "config") / "glab" / "corp")
    assert logins[0].folder == (tmp_path / "config") / "glab" / "corp"
    assert [f.id for f in started[0].forges] == ["github", "corp"]


async def test_a_forge_the_service_cannot_start_with_is_not_kept(
    site, tmp_path: Path
) -> None:
    app, logins, started, fail = site
    fail.append(RuntimeError("gitlab.example.com did not answer"))
    async with _signed_in(app, "maria") as maria:
        failed = await maria.post("/api/settings/forges", json={**CORP, "token": PAT})
        assert failed.status_code == 502 and "did not answer" in failed.json()["detail"]
        wrong = await maria.post(
            "/api/settings/forges", json={**CORP, "id": "other", "token": "glpat-no"}
        )
        assert wrong.status_code == 401
    assert [f.id for f in load_host_config(tmp_path / "config").forges] == ["github"]
    assert [login.discarded for login in logins] == [True, True]


async def test_a_forge_signed_in_with_a_one_time_code(site, tmp_path: Path) -> None:
    app, logins, started, _ = site
    async with _signed_in(app, "maria") as maria:
        begun = await maria.post("/api/settings/forges", json=CORP)
        assert begun.json()["code"] == "WXYZ-1234"
        flow = {"flow_id": begun.json()["flow_id"]}
        assert (
            await maria.post("/api/settings/forges/poll", json=flow)
        ).status_code == 202
        async with _signed_in(app, "walt") as walt:
            assert (
                await walt.post("/api/settings/forges/poll", json=flow)
            ).status_code == 403
        logins[0].approve()
        done = await maria.post("/api/settings/forges/poll", json=flow)
        assert done.status_code == 200 and done.json()["done"] is True
        assert (
            await maria.post("/api/settings/forges/poll", json=flow)
        ).status_code == 404
        listed = (await maria.get("/api/settings/forges")).json()["forges"]
        assert [(f["id"], f["default"]) for f in listed] == [
            ("github", True),
            ("corp", False),
        ]
    assert [f.id for f in load_host_config(tmp_path / "config").forges] == [
        "github",
        "corp",
    ]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"id": "Corp"}, "lowercase"),
        ({"id": "github"}, "already"),
        ({"web_url": "gitlab.example.com"}, "not a web address"),
        ({"kind": "github", "token": PAT}, "one-time code"),
    ],
)
async def test_a_forge_that_cannot_be_named_or_reached_is_refused(
    site, body: dict[str, str], detail: str
) -> None:
    app, logins, _, _ = site
    async with _signed_in(app, "maria") as maria:
        refused = await maria.post("/api/settings/forges", json={**CORP, **body})
        assert refused.status_code in (409, 422) and detail in refused.json()["detail"]
    assert logins == []


def test_each_forge_signs_in_into_a_folder_of_its_own(tmp_path: Path) -> None:
    save_host_config(HostConfig(data_dir=str(tmp_path / "data")), tmp_path)
    steps = ForgeSteps(tmp_path, tmp_path / "data")
    ghe = steps.new("ghe", "github", "https://ghe.example.com")
    assert ghe.cli_config_dir == str(tmp_path / "gh-ghe")
    assert ghe.api == "https://ghe.example.com/api/v3"
    assert steps.cli_folder("github", "github", default=True) == tmp_path / "gh"
