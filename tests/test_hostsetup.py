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

"""Host setup in the browser: the config the service writes, the service's repository
and with it the main forge (GitHub or GitLab), who may set a new host up (an admin of
that repository, who then owns it), the checks that let self-improvement work, the
memory repository, sign-in links, and who may change the setup later."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import stat
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from auto_maintainer import cli, ghcli
from auto_maintainer.config import (
    CONFIG_VERSION,
    HostConfig,
    legacy_github,
    load_host_config,
    save_host_config,
    set_host_secret,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.forges import registry
from auto_maintainer.forges.base import AccountError, ForgeAccount, RepoAccess
from auto_maintainer.forges.github.auth import StaticTokenProvider
from auto_maintainer.hostsetup import (
    GhLogin,
    HostSetup,
    SetupError,
    parse_link,
)
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web import app as web_app
from auto_maintainer.web import auth
from auto_maintainer.web.app import create_app
from auto_maintainer.web.auth import create_link
from auto_maintainer.web.setup import create_setup_app

TOKEN = "gho_" + "x" * 36
"""hao, an admin of hao/auto-maintainer."""
STRANGER = "gho_" + "y" * 36
"""eve, who can only read it."""
BOT = "gho_" + "b" * 36
"""am-bot, an account for the service to act as."""
ADA = "gho_" + "a" * 36
"""ada, another admin of the service's repository."""
NARROW = "github_pat_" + "n" * 36
"""A token of hao's that was not given access to the memory repository."""
UPSTREAM = "RLinf/auto-maintainer"
FORK = "hao/auto-maintainer"


@pytest.fixture(autouse=True)
def _restore_environment():
    """``set_host_secret`` also sets the process environment; undo it after each test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class World:
    """A fake forge for both kinds: people by token, and repositories with each
    person's role, privacy, and commits."""

    def __init__(self, head: str) -> None:
        self.people = {
            TOKEN: "hao",
            STRANGER: "eve",
            BOT: "am-bot",
            ADA: "ada",
            NARROW: "hao",
        }
        self.blind = {NARROW}
        """Tokens that see no private repository."""
        self.repos: dict[str, dict[str, Any]] = {
            UPSTREAM: {
                "private": False,
                "roles": {"eve": Permission.READ},
                "commits": {head},
            },
            FORK: {
                "private": False,
                "roles": {"hao": Permission.ADMIN, "ada": Permission.ADMIN},
                "commits": {head},
            },
        }

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        account = ForgeAccount(
            repo=self.repo,
            has_commit=self.has_commit,
            create_private=self.create_private,
            copy=self.copy,
        )
        for name in ("github", "gitlab"):
            monkeypatch.setitem(
                registry.KINDS,
                name,
                dataclasses.replace(
                    registry.KINDS[name], who=self.who, account=account
                ),
            )

    async def who(self, api: str, token: str) -> dict[str, str]:
        if token not in self.people:
            raise RuntimeError("401")
        login = self.people[token]
        return {"login": login, "name": login.title(), "avatar_url": ""}

    async def repo(self, api: str, token: str, path: str) -> RepoAccess | None:
        found = self.repos.get(path)
        login = self.people.get(token)
        if found is None or login is None:
            return None
        role = found["roles"].get(login, Permission.NONE)
        if found["private"] and (role is Permission.NONE or token in self.blind):
            return None
        return RepoAccess(
            path=path,
            private=found["private"],
            permission=role if role is not Permission.NONE else Permission.READ,
            default_branch="main",
            web_url=f"{api}/{path}",
        )

    async def has_commit(self, api: str, token: str, path: str, sha: str) -> bool:
        return sha in self.repos[path]["commits"]

    async def create_private(
        self, api: str, token: str, path: str, description: str
    ) -> RepoAccess:
        self.repos[path] = {
            "private": True,
            "roles": {self.people[token]: Permission.ADMIN},
            "commits": set(),
        }
        found = await self.repo(api, token, path)
        assert found is not None
        return found

    async def copy(
        self, api: str, token: str, source_web_url: str, source_path: str | None
    ) -> RepoAccess:
        login = self.people[token]
        source = self.repos.get(source_path or "")
        if source is None:
            raise AccountError("no such repository")
        path = f"{login}/{(source_path or '').rsplit('/', 1)[-1]}"
        self.repos[path] = {
            "private": False,
            "roles": {login: Permission.ADMIN},
            "commits": set(source["commits"]),
        }
        found = await self.repo(api, token, path)
        assert found is not None
        return found


class FakeLogin:
    """The forge CLI's sign-in: a one-time code the test approves with a token, or a
    token given at once; the token lands in its folder."""

    made: list[FakeLogin] = []

    def __init__(self, kind: str, config_dir: Path, web_url: str, **kw: Any) -> None:
        self.kind = kind
        self.config_dir = Path(config_dir)
        self.web_url = web_url
        self.client_id = kw.get("client_id")
        self.started = False
        FakeLogin.made.append(self)

    async def start(self) -> dict[str, str]:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.started = True
        return {"code": "ABCD-1234", "url": f"{self.web_url}/login/device"}

    def approve(self, token: str) -> None:
        (self.config_dir / "token").write_text(token)

    async def with_token(self, token: str) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.approve(token.strip())

    def state(self) -> str:
        if not self.started and not (self.config_dir / "token").exists():
            return "idle"
        return "done" if (self.config_dir / "token").exists() else "pending"

    async def token(self) -> str | None:
        path = self.config_dir / "token"
        return path.read_text() if path.exists() else None

    def discard(self) -> None:
        import shutil

        shutil.rmtree(self.config_dir, ignore_errors=True)

    def cancel(self) -> None:
        pass


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    """This host's checkout, cloned from ``FORK`` on github.com, and its commit."""
    path = tmp_path / "checkout"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("am\n")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "init",
    )
    _git(
        path,
        "remote",
        "add",
        "origin",
        f"https://tok:x@github.com/{FORK}.git",
    )
    return path, _git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(checkout, monkeypatch) -> World:
    w = World(checkout[1])
    w.install(monkeypatch)
    return w


def _setup(
    tmp_path: Path, checkout: tuple[Path, str], host: str | None = None
) -> HostSetup:
    async def host_token(kind: str, web_url: str) -> str | None:
        return host

    return HostSetup(
        tmp_path / "config",
        checkout=checkout[0],
        host_token=host_token,
        login_factory=FakeLogin,
    )


async def _sign_in(s: HostSetup, role: str, token: str, **kw: Any) -> dict[str, Any]:
    await s.signin_start(role)
    s._login.approve(token)
    return await s.signin_poll(**kw)


# -- the files the service writes ------------------------------------------------------


def test_the_service_writes_its_config_and_secrets(tmp_path: Path) -> None:
    cfg = HostConfig(base_url="http://box:7777", code_repo=FORK)
    cfg.default_forge.cli_config_dir = "/svc/gh"
    path = save_host_config(cfg, tmp_path)
    text = path.read_text()
    assert text.startswith("# Written by auto-maintainer")
    assert yaml.safe_load(text) == {
        "version": CONFIG_VERSION,
        "base_url": "http://box:7777",
        "forges": [{"id": "github", "kind": "github", "cli_config_dir": "/svc/gh"}],
        "code_repo": FORK,
    }
    set_host_secret("AM_WEBHOOK_SECRET", "one", tmp_path)
    set_host_secret("AM_WEBHOOK_SECRET", "two", tmp_path)
    secrets = tmp_path / "secrets.env"
    assert secrets.read_text() == "AM_WEBHOOK_SECRET=two\n"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert load_host_config(tmp_path).default_forge.cli_config_dir == "/svc/gh"
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


def test_a_link_names_a_repository_on_its_forge_without_credentials() -> None:
    assert parse_link("https://tok:x@github.com/RLinf/auto-maintainer.git") == (
        "https://github.com",
        "RLinf/auto-maintainer",
    )
    assert parse_link("git@github.com:acme/widget.git") == (
        "https://github.com",
        "acme/widget",
    )
    assert parse_link("ssh://git@gitlab.corp:2222/team/sub/am.git") == (
        "https://gitlab.corp",
        "team/sub/am",
    )
    assert parse_link("https://gitlab.com/team/am/-/tree/main") == (
        "https://gitlab.com",
        "team/am",
    )
    assert parse_link("http://10.0.0.5:8080/a/b") == ("http://10.0.0.5:8080", "a/b")
    assert parse_link("acme/widget") is None
    assert parse_link("https://github.com/acme") is None


# -- the service's repository and its owner --------------------------------------------


async def test_the_owner_is_an_admin_of_the_service_repo_who_signed_in(
    tmp_path: Path, checkout, world
) -> None:
    s = _setup(tmp_path, checkout)
    await s.set_repo(f"https://github.com/{FORK}")
    refused = await _sign_in(s, "owner", STRANGER)
    assert refused["state"] == "failed" and "eve is not an admin" in refused["error"]
    assert s.claimer is None and not (tmp_path / "config" / "gh").exists()
    done = await _sign_in(s, "owner", TOKEN)
    assert done["state"] == "done" and s.claimer["login"] == "hao"
    assert [c["id"] for c in s.checks if c["ok"]] == ["owner", "code", "push"]
    with pytest.raises(SetupError, match="no agent named 'devin'"):
        s.finish("http://box:7777/", "devin")
    cfg = s.finish("http://box:7777/", "antigravity")
    assert (cfg.code_repo, cfg.owner, cfg.operators) == (FORK, "hao", ["hao"])
    saved = load_host_config(tmp_path / "config")
    assert saved.owner == "hao" and saved.default_forge.kind == "github"
    assert saved.default_forge.cli_config_dir == str(tmp_path / "config" / "gh")
    assert (tmp_path / "config" / "gh" / "token").read_text() == TOKEN


async def test_a_repo_the_service_cannot_push_to_is_refused_until_it_can(
    tmp_path: Path, checkout, world
) -> None:
    """Self-improvement pushes branches as the service's account, so setup checks it
    can, also when the service acts as another account than the owner."""
    s = _setup(tmp_path, checkout)
    await s.set_repo(f"https://github.com/{FORK}")
    await _sign_in(s, "owner", TOKEN)
    done = await _sign_in(s, "service", BOT)
    assert done["state"] == "done" and s.identity["login"] == "am-bot"
    assert s.claimer["login"] == "hao"
    [push] = [c for c in s.checks if c["id"] == "push"]
    assert not push["ok"] and "am-bot cannot push" in push["text"]
    with pytest.raises(SetupError, match="am-bot cannot push"):
        s.finish("http://box:7777")
    world.repos[FORK]["roles"]["am-bot"] = Permission.WRITE
    s.checks = await s.verify()
    assert s.finish("http://box:7777").owner == "hao"


async def test_a_repo_without_this_hosts_code_is_refused_and_a_fork_has_it(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["hao/empty"] = {
        "private": False,
        "roles": {"hao": Permission.ADMIN},
        "commits": set(),
    }
    s = _setup(tmp_path, checkout)
    await s.set_repo("https://github.com/hao/empty")
    await _sign_in(s, "owner", TOKEN)
    [code] = [c for c in s.checks if c["id"] == "code"]
    assert not code["ok"] and "does not hold the code" in code["text"]
    s._checkout = {
        "web_url": "https://github.com",
        "path": UPSTREAM,
        "link": f"https://github.com/{UPSTREAM}",
        "kind": "github",
    }
    made = await s.copy_upstream()
    assert made == {"repo": FORK} and all(c["ok"] for c in s.checks)


async def test_someone_with_the_setup_link_owns_the_copy_they_make(
    tmp_path: Path, checkout, world
) -> None:
    """Cloned from upstream, whose admin they are not, the person with the setup link
    signs in, makes their own copy, and owns it."""
    s = _setup(tmp_path, checkout)
    s._checkout = {
        "web_url": "https://github.com",
        "path": UPSTREAM,
        "link": f"https://github.com/{UPSTREAM}",
        "kind": "github",
    }
    await s.set_repo(f"https://github.com/{UPSTREAM}")
    out = await _sign_in(s, "owner", TOKEN, trusted=True)
    assert out["person"]["owner"] is False and s.claimer is None
    assert s.summary()["candidate"]["login"] == "hao"
    del world.repos[FORK]
    assert await s.copy_upstream() == {"repo": FORK}
    assert s.claimer["login"] == "hao" and all(c["ok"] for c in s.checks)
    assert s.finish("http://box:7777").owner == "hao"


async def test_a_gitlab_repository_makes_gitlab_the_main_forge(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["team/auto-maintainer"] = world.repos.pop(FORK)
    s = _setup(tmp_path, checkout)
    with pytest.raises(SetupError, match="GitHub or GitLab"):
        await s.set_repo("https://git.corp.example/team/auto-maintainer")
    out = await s.set_repo(
        "https://git.corp.example/team/auto-maintainer",
        kind="gitlab",
        oauth_client_id="",
    )
    assert out["forge"] == {
        "id": "gitlab",
        "kind": "gitlab",
        "web_url": "https://git.corp.example",
        "device": False,
    }
    with pytest.raises(SetupError, match="GitHub signs in"):
        await _setup(tmp_path / "gh-host", checkout).signin_token("owner", TOKEN)
    done = await s.signin_token("owner", TOKEN, may_claim=True)
    assert done["person"]["login"] == "hao"
    cfg = s.finish("https://am.corp.example")
    [forge] = cfg.forges
    assert (forge.id, forge.kind, forge.web, forge.api) == (
        "gitlab",
        "gitlab",
        "https://git.corp.example",
        "https://git.corp.example/api/v4",
    )
    assert forge.cli_config_dir == str(tmp_path / "config" / "glab" / "gitlab")
    assert (cfg.code_repo, cfg.owner) == ("team/auto-maintainer", "hao")


async def test_the_host_cli_login_can_act_for_the_service(
    tmp_path: Path, checkout, world
) -> None:
    s = _setup(tmp_path, checkout, host=BOT)
    await s.set_repo(f"https://github.com/{FORK}")
    assert (await s.use_host_login())["login"] == "am-bot"
    with pytest.raises(SetupError, match="sign in as an admin"):
        s.finish("http://box:7777")
    world.repos[FORK]["roles"]["am-bot"] = Permission.WRITE
    await _sign_in(s, "owner", TOKEN)
    await _sign_in(s, "service", BOT)
    assert s.finish("http://box:7777").default_forge.cli_config_dir


async def test_connecting_the_github_cli_clears_what_an_app_left(
    tmp_path: Path, checkout, world
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
    s = _setup(tmp_path, checkout, host=TOKEN)
    assert s.summary()["reconnect"] == "a GitHub App"
    with pytest.raises(SetupError, match="sign the service in"):
        s.finish("http://box:7777")
    await s.use_host_login()
    s.finish("http://box:7777")
    assert legacy_github(config) is None and s.summary()["reconnect"] is None
    assert (config / "secrets.env").read_text() == "AM_WEBHOOK_SECRET=hook\n"
    assert not (config / "github-app.pem").exists()
    assert "app_id" not in (config / "local.yaml").read_text()


# -- the memory repository --------------------------------------------------------------


async def test_the_memory_repository_must_be_private(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["hao/open"] = {
        "private": False,
        "roles": {"hao": Permission.ADMIN},
        "commits": set(),
    }
    s = _setup(tmp_path, checkout)
    await s.set_repo(f"https://github.com/{FORK}")
    await _sign_in(s, "owner", TOKEN)
    with pytest.raises(SetupError, match="public"):
        await s.memory("hao/open", create=False)
    with pytest.raises(SetupError, match="does not exist"):
        await s.memory("hao/missing", create=False)
    assert (await s.memory("hao/am-memory", create=True))["created"] is True
    assert (await s.memory(None, create=False)) == {"repo": None}


async def test_a_memory_repo_a_sign_in_cannot_see_is_found_with_another_credential(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["hao/mem"] = {
        "private": True,
        "roles": {"hao": Permission.ADMIN, "am-bot": Permission.WRITE},
        "commits": set(),
    }
    s = _setup(tmp_path, checkout, host=BOT)
    await s.use_host_login()
    out = await s.memory("hao/mem", create=False, person_token=NARROW)
    assert out == {"repo": "hao/mem", "created": False}


async def test_a_memory_repo_nobody_can_see_names_the_accounts_tried(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["hao/mem"] = {"private": True, "roles": {}, "commits": set()}
    s = _setup(tmp_path, checkout, host=BOT)
    await s.use_host_login()
    with pytest.raises(SetupError) as e:
        await s.memory("hao/mem", create=False, person_token=NARROW)
    assert "hao" in str(e.value) and "am-bot" in str(e.value)


async def test_the_services_own_account_must_write_to_the_memory_repo(
    tmp_path: Path, checkout, world
) -> None:
    world.repos["hao/mem"] = {
        "private": True,
        "roles": {"hao": Permission.ADMIN, "eve": Permission.READ},
        "commits": set(),
    }
    s = _setup(tmp_path, checkout, host=STRANGER)
    await s.use_host_login()
    with pytest.raises(SetupError, match="eve cannot write to it"):
        await s.memory("hao/mem", create=False, person_token=TOKEN)


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
if [ "$1 $2" = "auth refresh" ]; then
  echo "$@" > "$GH_CONFIG_DIR/refresh-args"
  echo "! First copy your one-time code: WXYZ-9876"
  echo "Press Enter to open https://github.com/login/device in your browser..."
  while [ ! -f "$GH_CONFIG_DIR/approve-scope" ]; do sleep 0.05; done
  echo "$TOKEN-with-workflow" > "$GH_CONFIG_DIR/hosts"
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


# -- the first-run page ---------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get("/")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def _web_sign_in(
    client: httpx.AsyncClient, s: HostSetup, role: str, token: str
) -> Any:
    started = await client.post("/api/setup/signin/start", json={"role": role})
    assert started.status_code == 200, started.text
    s._login.approve(token)
    return await client.post("/api/setup/signin/poll")


async def test_first_run_is_owned_by_the_admin_who_signs_in(
    tmp_path: Path, checkout, world
) -> None:
    s = _setup(tmp_path, checkout)
    finished: list[HostConfig] = []

    async def on_finish(cfg: HostConfig) -> None:
        finished.append(cfg)

    app = create_setup_app(s, data_dir=tmp_path / "data", on_finish=on_finish)
    async with _client(app) as eve:
        assert (await eve.get("/api/me")).status_code == 503
        status = (await eve.get("/api/setup/status")).json()
        assert status["first_run"] and "user" not in status["detected"]["cli"]
        assert status["detected"]["checkout"]["path"] == FORK
        assert (await eve.post("/api/setup/host-login")).status_code == 401
        early = await eve.post(
            "/api/setup/finish", json={"base_url": "http://box:7777"}
        )
        assert early.status_code == 401
        await eve.post("/api/setup/repo", json={"link": f"https://github.com/{FORK}"})
        refused = (await _web_sign_in(eve, s, "owner", STRANGER)).json()
        assert refused["state"] == "failed"
    async with _client(app) as hao:
        claimed = (await _web_sign_in(hao, s, "owner", TOKEN)).json()
        assert claimed["state"] == "done" and claimed["person"]["login"] == "hao"
        assert (
            await hao.post("/api/setup/memory", json={"repo": None})
        ).status_code == 200
        async with _client(app) as late:
            assert (await late.get("/api/setup/status")).json()["busy"] == "hao"
            blocked = await late.post(
                "/api/setup/finish", json={"base_url": "http://box:7777"}
            )
            assert blocked.status_code == 403
        done = await hao.post("/api/setup/finish", json={"base_url": "http://box:7777"})
        assert done.status_code == 200 and "am_session" in done.headers.get(
            "set-cookie", ""
        )
    assert [(c.code_repo, c.owner) for c in finished] == [(FORK, "hao")]


async def test_without_the_setup_link_only_the_checkouts_repo_can_be_claimed(
    tmp_path: Path, checkout, world
) -> None:
    """Anyone who reaches a new host could fork the code, so without the link from
    the terminal only an admin of the repository this checkout came from claims it."""
    world.repos["eve/auto-maintainer"] = {
        "private": False,
        "roles": {"eve": Permission.ADMIN},
        "commits": set(world.repos[FORK]["commits"]),
    }
    s = _setup(tmp_path, checkout)
    app = create_setup_app(s, data_dir=tmp_path / "data", on_finish=lambda cfg: None)
    async with _client(app) as eve:
        await eve.post(
            "/api/setup/repo", json={"link": "https://github.com/eve/auto-maintainer"}
        )
        assert (await eve.get("/api/setup/status")).json()["may_claim"] is False
        out = (await _web_sign_in(eve, s, "owner", STRANGER)).json()
        assert out["state"] == "failed" and "setup link" in out["error"]
        link = await eve.get(f"/setup/start?code={s.code()}")
        assert link.status_code == 303
        assert (await eve.get("/api/setup/status")).json()["may_claim"] is True
        out = (await _web_sign_in(eve, s, "owner", STRANGER)).json()
        assert out["state"] == "done" and s.claimer["login"] == "eve"


# -- after setup ---------------------------------------------------------------------------


@pytest.fixture
async def running(tmp_path: Path, checkout, world):
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    h.forge.set_permission("hao/auto-maintainer", "ada", Permission.ADMIN)
    h.service.settings.code_repo = FORK
    h.service.settings.owner = "maria"
    h.forge.set_permission(FORK, "maria", Permission.ADMIN)
    save_host_config(
        HostConfig(code_repo=FORK, owner="maria", base_url="http://box:7777"),
        tmp_path / "config",
    )
    steps = _setup(tmp_path, checkout)
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
    yield h, app, changed, steps
    await h.close()


@contextlib.asynccontextmanager
async def _signed_in(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://box"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_only_the_owner_changes_the_host_setup(running) -> None:
    _, app, _, _ = running
    async with _signed_in(app, "ada") as ada:
        assert (await ada.get("/api/setup/status")).status_code == 403
    async with _signed_in(app, "maria") as maria:
        status = await maria.get("/api/setup/status")
        assert status.status_code == 200 and status.json()["first_run"] is False


async def test_the_setup_link_lets_an_admin_take_ownership(running) -> None:
    """When the owner cannot act, whoever holds the link from the host lets an admin
    of the service's repository sign in and become its owner; nobody else."""
    _, app, changed, steps = running
    async with _signed_in(app, "ada") as ada:
        assert (
            (await ada.get("/setup/start?code=wrong"))
            .headers["location"]
            .endswith("error=code")
        )
        await ada.get(f"/setup/start?code={steps.code()}")
        assert (await ada.get("/api/setup/status")).status_code == 200
        early = await ada.post(
            "/api/setup/finish", json={"base_url": "http://box:7777"}
        )
        assert early.status_code == 403 and "sign in as an admin" in early.text
        eve = (await _web_sign_in(ada, steps, "owner", STRANGER)).json()
        assert eve["person"]["owner"] is False and steps.claimer is None
        refused = await ada.post(
            "/api/setup/finish", json={"base_url": "http://box:7777"}
        )
        assert refused.status_code == 403
        out = (await _web_sign_in(ada, steps, "owner", ADA)).json()
        assert out["state"] == "done"
        done = await ada.post("/api/setup/finish", json={"base_url": "http://box:7777"})
        assert done.status_code == 200
    assert changed[-1].owner == "ada"


async def test_the_memory_repo_is_made_with_the_owners_own_account(running) -> None:
    h, app, _, _ = running
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
    _, app, _, _ = running
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


def test_a_version_2_file_becomes_the_default_github_forge(tmp_path: Path) -> None:
    (tmp_path / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "github": {
                    "gh_config_dir": "/svc/gh",
                    "web_url": "https://ghe.corp",
                    "api_url": "https://ghe.corp/api/v3",
                    "auth": "gh",
                },
            }
        )
    )
    cfg = load_host_config(tmp_path)
    [forge] = cfg.forges
    assert (forge.id, forge.kind, forge.cli_config_dir) == (
        "github",
        "github",
        "/svc/gh",
    )
    assert (forge.web, forge.api, forge.host) == (
        "https://ghe.corp",
        "https://ghe.corp/api/v3",
        "ghe.corp",
    )
    gitlab = HostConfig.model_validate(
        {"forges": [{"id": "github"}, {"id": "corp", "kind": "gitlab"}]}
    ).forge("corp")
    assert (gitlab.web, gitlab.api) == (
        "https://gitlab.com",
        "https://gitlab.com/api/v4",
    )
    with pytest.raises(ValueError, match="each id once"):
        HostConfig.model_validate({"forges": [{"id": "a"}, {"id": "a"}]})


async def test_stopping_the_server_stops_a_sign_in_in_progress(
    tmp_path: Path, checkout, world
) -> None:
    s = _setup(tmp_path, checkout)
    s.cancel()  # nothing in progress
    await s.set_repo(f"https://github.com/{FORK}")
    await s.signin_start("owner")
    cancelled: list[bool] = []
    s._login.cancel = lambda: cancelled.append(True)
    s.cancel()
    assert cancelled == [True]


async def test_a_github_sign_in_asks_for_the_workflow_scope(
    tmp_path: Path, fake_gh
) -> None:
    login = GhLogin(tmp_path / "gh")
    shown = await login.start()
    assert shown["code"] == "ABCD-1234"
    args = (tmp_path / "gh" / "args").read_text()
    assert "--scopes=workflow" in args
    login.cancel()


async def test_the_services_sign_in_gets_a_scope_added_and_read_again(
    tmp_path: Path, fake_gh
) -> None:
    from auto_maintainer.forges.github.auth import ScopeRequest

    folder = tmp_path / "svc-gh"
    folder.mkdir()
    (folder / "hosts").write_text(TOKEN + "\n")
    tokens = StaticTokenProvider(None, gh_config_dir=str(folder))
    assert await tokens.token() == TOKEN
    grant = ScopeRequest("github.com", str(folder), ["workflow"])
    shown = await grant.start()
    assert shown == {"code": "WXYZ-9876", "url": "https://github.com/login/device"}
    assert "--scopes workflow" in (folder / "refresh-args").read_text()
    assert grant.state() == "pending"
    (folder / "approve-scope").write_text("")
    assert await grant.wait() is True and grant.state() == "done"
    tokens.forget()
    assert await tokens.token() == TOKEN + "-with-workflow"
