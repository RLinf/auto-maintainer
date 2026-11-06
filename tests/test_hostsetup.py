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

"""Host setup in the browser: the config the service writes, who may set a new host
up (a maintainer of the service's repository, proven through GitHub), the GitHub CLI
login run for the browser, sign-in links, and who may change the setup later."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from auto_maintainer import cli, ghcli
from auto_maintainer.config import (
    HostConfig,
    legacy_github,
    load_host_config,
    save_host_config,
    set_host_secret,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.github.auth import StaticTokenProvider
from auto_maintainer.hostsetup import GhLogin, HostSetup, SetupError, repo_from_remote
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web import app as web_app
from auto_maintainer.web import auth
from auto_maintainer.web.app import create_app
from auto_maintainer.web.auth import create_link
from auto_maintainer.web.setup import create_setup_app

TOKEN = "gho_" + "x" * 36
"""hao, a maintainer of acme/auto-maintainer."""
STRANGER = "gho_" + "y" * 36
"""eve, who can only read it."""
SERVICE_REPO = "acme/auto-maintainer"
USERS = {TOKEN: "hao", STRANGER: "eve"}


@pytest.fixture(autouse=True)
def _restore_environment():
    """``set_host_secret`` also sets the process environment; undo it after each test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _github(repos: dict[str, dict[str, Any]] | None = None) -> httpx.MockTransport:
    """A fake GitHub: two users, the repositories given, and the service's repository."""
    repos = dict(repos or {})

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        path = request.url.path
        if path == "/user":
            if token not in USERS:
                return httpx.Response(401, json={"message": "Bad credentials"})
            login = USERS[token]
            return httpx.Response(
                200,
                json={"login": login, "name": login.title(), "avatar_url": ""},
                headers={"x-oauth-scopes": "repo, read:org"},
            )
        if path == "/user/repos" and request.method == "POST":
            body = json.loads(request.content)
            repos[f"hao/{body['name']}"] = {"private": body["private"]}
            return httpx.Response(201, json={"full_name": f"hao/{body['name']}"})
        if path == f"/repos/{SERVICE_REPO}":
            maintain = USERS.get(token) == "hao"
            return httpx.Response(
                200,
                json={
                    "private": False,
                    "permissions": {
                        "maintain": maintain,
                        "push": maintain,
                        "pull": True,
                    },
                },
            )
        if path.startswith("/repos/"):
            name = path.removeprefix("/repos/")
            if name in repos:
                return httpx.Response(200, json=repos[name])
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def _gh_logged_in(args: list[str]) -> tuple[int, str]:
    return (0, TOKEN) if args == ["auth", "token"] else (1, "")


async def _gh_logged_out(args: list[str]) -> tuple[int, str]:
    return 1, ""


def _gh_as(token: str):
    """The host's GitHub CLI, logged in with ``token``."""

    async def run(args: list[str]) -> tuple[int, str]:
        return (0, token) if args == ["auth", "token"] else (1, "")

    return run


def _setup(tmp_path: Path, **kw: Any) -> HostSetup:
    s = HostSetup(
        tmp_path / "config",
        checkout=tmp_path,
        run_gh=kw.pop("run_gh", _gh_logged_in),
        transport=kw.pop("transport", _github()),
    )
    s._checkout_repo = kw.pop("checkout_repo", SERVICE_REPO)
    return s


# -- the files the service writes ------------------------------------------------------


def test_the_service_writes_its_config_and_secrets(tmp_path: Path) -> None:
    cfg = HostConfig(base_url="http://box:7777", code_repo=SERVICE_REPO)
    cfg.github.gh_config_dir = "/svc/gh"
    path = save_host_config(cfg, tmp_path)
    text = path.read_text()
    assert text.startswith("# Written by auto-maintainer")
    assert yaml.safe_load(text) == {
        "base_url": "http://box:7777",
        "github": {"gh_config_dir": "/svc/gh"},
        "code_repo": SERVICE_REPO,
    }
    set_host_secret("AM_WEBHOOK_SECRET", "one", tmp_path)
    set_host_secret("AM_WEBHOOK_SECRET", "two", tmp_path)
    secrets = tmp_path / "secrets.env"
    assert secrets.read_text() == "AM_WEBHOOK_SECRET=two\n"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert load_host_config(tmp_path).github.gh_config_dir == "/svc/gh"
    assert legacy_github(tmp_path) is None


def _old_setup(config: Path, github: dict[str, Any], secrets: str = "") -> None:
    """A ``local.yaml`` (and ``secrets.env``) from before the GitHub CLI was the only
    way to reach GitHub."""
    config.mkdir(parents=True, exist_ok=True)
    (config / "local.yaml").write_text(
        yaml.safe_dump({"base_url": "http://box:7777", "github": github})
    )
    if secrets:
        (config / "secrets.env").write_text(secrets)
        (config / "secrets.env").chmod(0o600)


def test_hosts_set_up_with_an_app_or_a_token_must_connect_again(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    _old_setup(
        app,
        {"auth": "app", "app_id": 42, "oauth_client_id": "Iv1.abc"},
        "AM_OAUTH_CLIENT_SECRET=s3cret\n",
    )
    assert legacy_github(app) == "a GitHub App"
    assert load_host_config(app).base_url == "http://box:7777"
    pasted = tmp_path / "pasted"
    _old_setup(pasted, {}, "GITHUB_TOKEN=" + TOKEN + "\n")
    assert legacy_github(pasted) == "a token"
    named = tmp_path / "named"
    _old_setup(named, {"auth": "token", "token_env": "MY_TOKEN"})
    assert legacy_github(named) == "a token"
    # Without a token kept for it, the old default already read the GitHub CLI's.
    _old_setup(tmp_path / "cli", {"auth": "gh"})
    _old_setup(tmp_path / "default", {})
    assert legacy_github(tmp_path / "cli") is None
    assert legacy_github(tmp_path / "default") is None


async def test_connecting_the_github_cli_clears_what_an_app_left(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    _old_setup(
        config,
        {
            "auth": "app",
            "app_id": 42,
            "private_key_path": str(config / "github-app.pem"),
        },
        "AM_OAUTH_CLIENT_SECRET=s3cret\nAM_WEBHOOK_SECRET=hook\n",
    )
    (config / "github-app.pem").write_text("KEY")
    s = _setup(tmp_path)
    assert s.summary()["reconnect"] == "a GitHub App"
    with pytest.raises(SetupError, match="connect"):
        s.finish("http://box:7777", None)
    await s.use_gh()
    s.finish("http://box:7777", None)
    assert legacy_github(config) is None and s.summary()["reconnect"] is None
    assert (config / "secrets.env").read_text() == "AM_WEBHOOK_SECRET=hook\n"
    assert not (config / "github-app.pem").exists()
    assert "app_id" not in (config / "local.yaml").read_text()


def test_a_remote_url_never_leaks_its_credentials() -> None:
    assert repo_from_remote("https://tok:x@github.com/RLinf/auto-maintainer.git") == (
        "RLinf/auto-maintainer"
    )
    assert repo_from_remote("git@github.com:acme/widget.git") == "acme/widget"
    assert repo_from_remote("https://gitlab.com/a/b") is None


# -- who owns a new host's setup ---------------------------------------------------------


async def test_a_maintainers_login_claims_the_setup_and_a_strangers_does_not(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path)
    with pytest.raises(SetupError, match="eve does not have maintain"):
        await s.claim(STRANGER)
    assert s.claimer is None
    assert (await s.claim(TOKEN))["login"] == "hao"
    await s.use_gh()
    cfg = s.finish("http://box:7777/", SERVICE_REPO)
    assert cfg.operators == ["hao"] and cfg.github.gh_config_dir is None


async def test_without_a_github_remote_only_the_code_proves_ownership(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path, checkout_repo=None)
    with pytest.raises(SetupError, match="setup link"):
        await s.claim(TOKEN)


async def test_the_memory_repository_must_be_private(tmp_path: Path) -> None:
    s = _setup(tmp_path, transport=_github({"hao/open": {"private": False}}))
    await s.claim(TOKEN)
    await s.use_gh()
    with pytest.raises(SetupError, match="public"):
        await s.memory("hao/open", create=False)
    with pytest.raises(SetupError, match="does not exist"):
        await s.memory("hao/missing", create=False)
    assert (await s.memory("hao/am-memory", create=True))["created"] is True
    assert (await s.memory(None, create=False)) == {"repo": None}


async def test_gh_mode_reads_only_the_github_cli(tmp_path: Path) -> None:
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\necho "from-gh $GH_CONFIG_DIR"\n')
    gh.chmod(0o755)
    os.environ["GITHUB_TOKEN"] = "from-env"
    own = StaticTokenProvider(None, gh_binary=str(gh), gh_config_dir="/svc/gh")
    assert await own.token() == "from-gh /svc/gh"
    assert (
        await StaticTokenProvider("GITHUB_TOKEN", gh_binary=str(gh)).token()
        == "from-env"
    )


# -- the GitHub CLI login, run for the browser ------------------------------------------------

FAKE_GH = """#!/bin/sh
if [ "$1 $2" = "auth login" ]; then
  echo "$GIT_CONFIG_GLOBAL" > "$GH_CONFIG_DIR/git-config-was"
  echo "$@" > "$GH_CONFIG_DIR/args"
  echo "! First copy your one-time code: ABCD-1234"
  echo "Open this URL to continue in your web browser: https://github.com/login/device"
  while [ ! -f "$GH_CONFIG_DIR/approve" ]; do sleep 0.05; done
  echo "$TOKEN" > "$GH_CONFIG_DIR/hosts"
  exit 0
fi
if [ "$1 $2" = "auth token" ]; then cat "$GH_CONFIG_DIR/hosts" 2>/dev/null || exit 1; fi
"""


@pytest.fixture
def fake_gh(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH.replace("$TOKEN", TOKEN))
    (bin_dir / "gh").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    return bin_dir


async def _approved(login: GhLogin) -> None:
    (login.config_dir / "approve").write_text("")
    for _ in range(100):
        if login.state() != "pending":
            return
        await asyncio.sleep(0.05)


async def test_the_github_cli_login_runs_in_the_browser(
    tmp_path: Path, fake_gh
) -> None:
    s = _setup(tmp_path)
    started = await s.gh_web_start()
    assert started == {"code": "ABCD-1234", "url": "https://github.com/login/device"}
    assert (await s.gh_web_poll(claim=True)) == {"state": "pending"}
    await _approved(s.gh_login)
    done = await s.gh_web_poll(claim=True)
    assert done["state"] == "done" and done["identity"]["login"] == "hao"
    own = tmp_path / "config" / "gh"
    assert (own / "git-config-was").read_text().strip() == str(own / "gitconfig")
    cfg = s.finish("http://box:7777", None)
    assert cfg.github.gh_config_dir == str(own)


# -- the first-run page ---------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get("/")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_first_run_is_owned_by_the_maintainer_who_proves_it(
    tmp_path: Path, fake_gh
) -> None:
    s = _setup(tmp_path)
    finished: list[HostConfig] = []

    async def on_finish(cfg: HostConfig) -> None:
        finished.append(cfg)

    app = create_setup_app(s, data_dir=tmp_path / "data", on_finish=on_finish)
    async with _client(app) as eve:
        assert (await eve.get("/api/me")).status_code == 503
        status = (await eve.get("/api/setup/status")).json()
        assert status["first_run"] and "user" not in status["detected"]["gh"]
        gh = await eve.post("/api/setup/github")
        assert gh.status_code == 403  # the host's own login needs the terminal code
        early = await eve.post(
            "/api/setup/finish", json={"base_url": "http://box:7777"}
        )
        assert early.status_code == 401
    async with _client(app) as hao:
        started = (await hao.post("/api/setup/gh/start")).json()
        assert started["code"] == "ABCD-1234"
        await _approved(s.gh_login)
        claimed = (await hao.post("/api/setup/gh/poll")).json()
        assert claimed["state"] == "done" and claimed["identity"]["login"] == "hao"
        assert (
            await hao.post("/api/setup/memory", json={"repo": None})
        ).status_code == 200
        async with _client(app) as late:
            assert (await late.get("/api/setup/status")).json()["busy"] == "hao"
            blocked = await late.post(
                "/api/setup/finish", json={"base_url": "http://box:7777"}
            )
            assert blocked.status_code == 403
        done = await hao.post(
            "/api/setup/finish", json={"base_url": "http://box:7777", "code_repo": None}
        )
        assert done.status_code == 200 and "am_session" in done.headers.get(
            "set-cookie", ""
        )
    assert [c.github.gh_config_dir for c in finished] == [str(s.gh_login.config_dir)]


# -- after setup ---------------------------------------------------------------------------


@pytest.fixture
async def running(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    steps = _setup(tmp_path, run_gh=_gh_logged_out)
    changed: list[Any] = []

    async def on_change(cfg: Any) -> None:
        changed.append(cfg)

    app = create_app(
        h.service,
        data_dir=tmp_path,
        allow_dev_login=True,
        links_path=tmp_path / "links.json",
        setup_steps=lambda: steps,
        on_host_change=on_change,
    )
    yield h, app, changed
    await h.close()


@contextlib.asynccontextmanager
async def _signed_in(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_only_maintainers_of_the_service_repo_change_the_host_setup(
    running,
) -> None:
    _, app, _ = running
    async with _signed_in(app, "walt") as walt:
        assert (await walt.get("/api/setup/status")).status_code == 403
    async with _signed_in(app, "maria") as maria:
        status = await maria.get("/api/setup/status")
        assert status.status_code == 200 and status.json()["first_run"] is False


async def test_the_memory_repo_is_made_with_the_maintainers_own_account(
    running,
) -> None:
    h, app, _ = running
    async with _signed_in(app, "maria") as maria:
        nothing = await maria.post(
            "/api/setup/memory", json={"repo": "hao/am-memory", "create": True}
        )
        assert nothing.status_code == 422 and "sign in" in nothing.json()["detail"]
        await h.service.a.user_tokens.set("maria", UserToken(token=TOKEN))
        made = await maria.post(
            "/api/setup/memory", json={"repo": "hao/am-memory", "create": True}
        )
        assert made.status_code == 200 and made.json()["created"] is True


async def test_a_sign_in_link_works_once(running, tmp_path: Path) -> None:
    _, app, _ = running
    url = create_link(tmp_path / "links.json", "maria", "http://box")
    path = url.removeprefix("http://box")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        first = await client.get(path)
        assert first.status_code == 303 and first.headers["location"] == "/"
        assert (await client.get("/api/me")).json()["login"] == "maria"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        again = await client.get(path)
        assert again.headers["location"] == "/?signin=expired"
        assert (await client.get("/api/me")).status_code == 401
    assert stat.S_IMODE((tmp_path / "links.json").stat().st_mode) == 0o600


async def test_signing_in_on_the_web_through_the_github_cli_keeps_nothing(
    tmp_path: Path, fake_gh, monkeypatch
) -> None:
    h = await build_demo(tmp_path)
    cfg = HostConfig(data_dir=str(tmp_path / "data"))
    left = cfg.data_path / "sign-in" / "from-a-run-before"
    left.mkdir(parents=True)
    (left / "hosts").write_text(TOKEN)
    factory = cli._gh_signin(cfg)
    assert not left.exists()
    logins: list[GhLogin] = []

    def tracked(flow_id: str) -> GhLogin:
        logins.append(factory(flow_id))
        return logins[-1]

    async def who(api_url: str, token: str) -> auth.User:
        assert token == TOKEN
        return auth.User(login="maria", name="Maria")

    monkeypatch.setattr(auth, "_github_user", who)
    app = create_app(h.service, data_dir=tmp_path, gh_signin=tracked)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        offered = (await client.get("/auth/config")).json()
        assert offered["gh"] is True and "oauth" not in offered
        assert (await client.get("/auth/login")).status_code == 404
        started = (await client.post("/auth/gh/start")).json()
        assert started["code"] == "ABCD-1234"
        folder = logins[0].config_dir
        # The token goes to the sign-in's own folder, never the keyring every gh shares.
        assert "--insecure-storage" in (folder / "args").read_text()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        pending = await client.post(
            "/auth/gh/poll", json={"flow_id": started["flow_id"]}
        )
        assert pending.status_code == 202
        await _approved(logins[0])
        done = await client.post("/auth/gh/poll", json={"flow_id": started["flow_id"]})
        assert done.status_code == 200 and done.json() == {"login": "maria"}
        assert (await client.get("/api/me")).json()["login"] == "maria"
        kept = await h.service.a.user_tokens.get("maria")
        assert kept is not None and kept.token == TOKEN and kept.source == "gh"
        await client.post("/auth/logout")
        assert await h.service.a.user_tokens.get("maria") is None
    assert not logins[0].config_dir.exists()
    await h.close()


async def test_without_gh_on_the_host_the_sign_in_page_says_what_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    h = await build_demo(tmp_path)
    factory = cli._gh_signin(HostConfig(data_dir=str(tmp_path / "data")))
    app = create_app(h.service, data_dir=tmp_path, gh_signin=factory)
    installed: list[Path] = []

    async def install(data: Path) -> str:
        installed.append(data)
        return str(data / "bin" / "gh")

    monkeypatch.setattr(ghcli, "install", install)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        monkeypatch.setattr(auth.shutil, "which", lambda name: None)
        offered = (await client.get("/auth/config")).json()
        assert offered["gh"] is False and offered["gh_missing"] is True
        assert (await client.post("/auth/gh/start")).status_code == 404
        done = (await client.post("/auth/gh/install")).json()
        assert done["installed"] is True and installed == [tmp_path]
        # Installing gh needs no restart: the next look finds it.
        monkeypatch.setattr(auth.shutil, "which", lambda name: "/usr/bin/gh")
        offered = (await client.get("/auth/config")).json()
        assert offered["gh"] is True and offered["gh_missing"] is False
    await h.close()


async def test_every_restart_serves_the_new_dashboard(
    tmp_path: Path, monkeypatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(web_app, "STATIC", static)
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        page = await client.get("/prs")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
    await h.close()


# -- a private memory repository not every credential can see ----------------------------

NARROW = "github_pat_" + "a" * 36
"""A token of maria's that was not given access to the memory repository."""
MEMORY = "RLinf/rlinf-am-mem"


def _private_github(visible_to: set[str]) -> httpx.MockTransport:
    """A fake GitHub whose memory repository only ``visible_to`` logins can see."""
    users = {**USERS, NARROW: "maria"}

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        login = users.get(token)
        if request.url.path == "/user":
            if login is None:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"login": login, "name": login.title()})
        if request.url.path == f"/repos/{MEMORY}":
            if login in visible_to and token != NARROW:
                return httpx.Response(200, json={"private": True})
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def test_a_memory_repo_a_sign_in_cannot_see_is_found_with_another_credential(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path, transport=_private_github({"hao", "maria"}))
    out = await s.memory(MEMORY, create=False, person_token=NARROW)
    assert out == {"repo": MEMORY, "created": False}
    assert s.cfg.memory.remote == MEMORY


async def test_a_memory_repo_nobody_can_see_names_the_accounts_tried(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path, transport=_private_github(set()))
    with pytest.raises(SetupError) as e:
        await s.memory(MEMORY, create=False, person_token=NARROW)
    message = str(e.value)
    assert "maria" in message and "hao" in message and "given access" in message


async def test_the_services_own_account_must_see_the_memory_repo(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path, transport=_private_github({"hao"}), run_gh=_gh_as(STRANGER))
    await s.use_gh()
    with pytest.raises(SetupError, match="service's account eve cannot see it"):
        await s.memory(MEMORY, create=False, person_token=TOKEN)
