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

"""Host setup in the browser (design 7.3): the service's GitHub account, sign-in, the
memory repository, and the address, written to ``local.yaml`` and ``secrets.env`` so
nobody edits a file or types a command.

Who may set up a new host is decided by GitHub, like every other configuration
change: the person must show that they maintain the service's own repository (the
checkout's ``origin``), by signing in through the GitHub App the page has GitHub
create, or with the token they paste. Until someone has, anyone who reaches the page
may start, and the first such maintainer owns the setup. The one-time code that
``am serve`` prints also proves ownership, for a checkout without a GitHub remote and
for using the GitHub CLI's login on the host. After setup, the same steps run from the
dashboard behind the service gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import jwt

from . import subproc
from .config import (
    CONFIG_DIR,
    HostConfig,
    is_configured,
    load_host_config,
    save_host_config,
    set_host_secret,
    write_private_file,
)

AGENT_BINARIES = {
    "claude-code": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "opencode": "opencode",
    "gemini": "gemini",
}

APP_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "statuses": "read",
    "actions": "write",
    "metadata": "read",
}
"""What the service needs on the repositories it maintains and on its memory repo."""

LEVELS = ["pull", "triage", "push", "maintain", "admin"]
"""GitHub's repository permission names, lowest first, as ``GET /repos`` reports them."""

RunGh = Callable[[list[str]], Awaitable[tuple[int, str]]]


class SetupError(ValueError):
    """A setup step cannot be done; the message says why in the user's terms."""


async def _run_gh(args: list[str]) -> tuple[int, str]:
    binary = shutil.which("gh")
    if binary is None:
        return 127, ""
    result = await subproc.run([binary, *args], timeout=20)
    return result.exit_code, result.stdout.strip()


def repo_from_remote(url: str) -> str | None:
    """``owner/repo`` from a GitHub remote URL, ignoring any credentials in it."""
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GhLogin:
    """``gh auth login --web`` run for a browser: GitHub's device flow, in which the
    person enters a one-time code on github.com and authorizes the GitHub CLI.

    The login lands in ``config_dir`` (``GH_CONFIG_DIR``), never in the host user's
    own GitHub CLI configuration, and git's global configuration is redirected into
    the same directory so the login cannot install itself as git's credential helper.

    ``throwaway`` is for a person signing in to the dashboard: the token is written to
    a file in ``config_dir`` (mode 0700) instead of the system keyring, which every
    ``gh`` on the host shares and which would keep it after ``discard()``.
    """

    CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
    URL = re.compile(r"(https://\S+/login/device)")

    def __init__(
        self,
        config_dir: Path,
        web_url: str = "https://github.com",
        *,
        throwaway: bool = False,
    ) -> None:
        self.config_dir = config_dir
        self.throwaway = throwaway
        self.host = web_url.split("://", 1)[-1].rstrip("/")
        self.proc: asyncio.subprocess.Process | None = None
        self.output: list[str] = []
        self._done: asyncio.Task[None] | None = None

    def env(self) -> dict[str, str]:
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return {
            **os.environ,
            "GH_CONFIG_DIR": str(self.config_dir),
            "GIT_CONFIG_GLOBAL": str(self.config_dir / "gitconfig"),
            "GH_PROMPT_DISABLED": "1",
            "BROWSER": "true",
            "NO_COLOR": "1",
        }

    async def start(self, timeout: float = 30) -> dict[str, str]:
        """Start the login and return the code and the page to enter it on."""
        binary = shutil.which("gh")
        if binary is None:
            raise SetupError("the GitHub CLI is not installed on this host")
        self.cancel()
        self.output = []
        self.proc = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "login",
            "--hostname",
            self.host,
            "--git-protocol",
            "https",
            "--web",
            "--skip-ssh-key",
            *(["--insecure-storage"] if self.throwaway else []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env(),
            start_new_session=True,
        )
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(b"\n")
        await self.proc.stdin.drain()
        code = url = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while code is None or url is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readline(), remaining)
            except TimeoutError:
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            self.output.append(line)
            code = code or (m.group(1) if (m := self.CODE.search(line)) else None)
            url = url or (m.group(1) if (m := self.URL.search(line)) else None)
        if code is None or url is None:
            self.cancel()
            raise SetupError(
                "the GitHub CLI did not start a login: " + " ".join(self.output[-3:])
            )
        self._done = asyncio.create_task(self._drain())
        return {"code": code, "url": url}

    async def _drain(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            self.output.append(raw.decode("utf-8", "replace").strip())
        await self.proc.wait()

    def state(self) -> Literal["idle", "pending", "done", "failed"]:
        if self.proc is None:
            return "idle"
        if self.proc.returncode is None:
            return "pending"
        return "done" if self.proc.returncode == 0 else "failed"

    async def token(self) -> str | None:
        binary = shutil.which("gh")
        if binary is None:
            return None
        result = await subproc.run(
            [binary, "auth", "token", "--hostname", self.host],
            env=self.env(),
            timeout=20,
        )
        return result.stdout.strip() if result.ok and result.stdout.strip() else None

    def discard(self) -> None:
        """Stop the login and delete everything it wrote (sign-in only needs the name)."""
        self.cancel()
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def cancel(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
        if self._done is not None:
            self._done.cancel()
        self.proc = None


class HostSetup:
    """One setup in progress. Choices stay in memory until ``finish`` writes them."""

    def __init__(
        self,
        config_dir: Path = CONFIG_DIR,
        *,
        checkout: Path | None = None,
        run_gh: RunGh = _run_gh,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.checkout = checkout or Path(__file__).resolve().parents[2]
        self.run_gh = run_gh
        self.transport = transport
        self.cfg = (
            load_host_config(config_dir) if is_configured(config_dir) else HostConfig()
        )
        self.identity: dict[str, Any] | None = None
        """The service's GitHub account once chosen: login, name, avatar, kind."""
        self.claimer: dict[str, Any] | None = None
        """The maintainer who proved ownership and owns this setup."""
        self.app: dict[str, Any] | None = None
        """The GitHub App GitHub created for this setup: slug, owner, purpose."""
        self.signin: Literal["gh", "app"] = (
            "app" if self.cfg.github.oauth_client_id else "gh"
        )
        """How people sign in: through a GitHub App, or by authorizing the GitHub CLI."""
        self._secrets: dict[str, str] = {}
        self._pem: str | None = None
        self._token: str | None = None
        self._manifest_state: dict[str, str] = {}
        self._checkout_repo: str | None | Literal[False] = False
        self._claimer_token: str | None = None
        self.gh_login = GhLogin(config_dir / "gh", self.cfg.github.web_url)
        """The service's own GitHub CLI login, made in the browser."""

    # -- the one-time code ---------------------------------------------------------------

    @property
    def code_path(self) -> Path:
        return self.config_dir / "setup-code"

    def code(self) -> str:
        """The setup code, created on first use and kept until setup finishes."""
        if self.code_path.is_file():
            return self.code_path.read_text(encoding="utf-8").strip()
        code = secrets.token_urlsafe(12)
        write_private_file(self.code_path, code)
        return code

    def check_code(self, code: str) -> bool:
        return self.code_path.is_file() and secrets.compare_digest(
            code.strip(), self.code_path.read_text(encoding="utf-8").strip()
        )

    # -- GitHub --------------------------------------------------------------------------

    def _client(self, base: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base or self.cfg.github.api_url,
            timeout=20,
            transport=self.transport,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "auto-maintainer",
            },
        )

    async def _user(self, token: str) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.get("/user", headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise SetupError("GitHub rejected this token")
        if r.status_code != 200:
            raise SetupError(f"GitHub answered {r.status_code} when asked who this is")
        data = r.json()
        scopes = [
            s.strip()
            for s in r.headers.get("x-oauth-scopes", "").split(",")
            if s.strip()
        ]
        return {
            "login": data["login"],
            "name": data.get("name") or data["login"],
            "avatar_url": data.get("avatar_url") or "",
            "scopes": scopes,
            "classic": "x-oauth-scopes" in r.headers,
        }

    async def gh_token(self) -> str | None:
        code, out = await self.run_gh(["auth", "token"])
        return out if code == 0 and out else None

    async def checkout_repo(self) -> str | None:
        """The service's own repository on GitHub, from the checkout's ``origin``."""
        if self._checkout_repo is not False:
            return self._checkout_repo
        repo = None
        if (self.checkout / ".git").exists():
            result = await subproc.run(
                ["git", "-C", str(self.checkout), "remote", "get-url", "origin"],
                timeout=10,
            )
            if result.exit_code == 0:
                repo = repo_from_remote(result.stdout)
        self._checkout_repo = repo
        return repo

    async def detect(self, *, private: bool) -> dict[str, Any]:
        """What this host has. ``private`` adds the GitHub CLI's account, which only
        the owner of the setup may see."""
        gh: dict[str, Any] = {"installed": shutil.which("gh") is not None}
        if private:
            token = await self.gh_token()
            if token:
                try:
                    gh["user"] = await self._user(token)
                except SetupError as e:
                    gh["error"] = str(e)
        return {
            "gh": gh,
            "agents": {
                name: bool(shutil.which(b)) for name, b in AGENT_BINARIES.items()
            },
            "checkout_repo": await self.checkout_repo(),
            "hostname": socket.gethostname(),
        }

    # -- ownership -------------------------------------------------------------------------

    async def claim(self, token: str) -> dict[str, Any]:
        """Make the person whose token this is the owner of the setup, if they maintain
        the service's own repository."""
        user = await self._user(token)
        repo = await self.checkout_repo()
        if repo is None:
            raise SetupError(
                "this checkout has no GitHub repository to check maintainers against; "
                "open the setup link that `am serve` printed instead"
            )
        need = self.cfg.self_update.approve_permission
        async with self._client() as client:
            r = await client.get(
                f"/repos/{repo}", headers={"Authorization": f"Bearer {token}"}
            )
        perms = r.json().get("permissions", {}) if r.status_code == 200 else {}
        have = max(
            (i for i, level in enumerate(LEVELS) if perms.get(level)), default=-1
        )
        wanted = LEVELS.index("push" if need == "write" else need)
        if have < wanted:
            raise SetupError(
                f"{user['login']} does not have {need} on {repo}; only its maintainers "
                "can set up this service"
            )
        if self.claimer and self.claimer["login"] != user["login"]:
            raise SetupError(f"{self.claimer['login']} is already setting this up")
        self.claimer = user
        self._claimer_token = token
        return user

    # -- the service's GitHub account ----------------------------------------------------------

    def _warning(self, user: dict[str, Any]) -> str:
        if user.get("classic") and "repo" not in user.get("scopes", []):
            return "this token has no repo scope, so it cannot read private repositories or post"
        return ""

    async def use_gh(self) -> dict[str, Any]:
        token = await self.gh_token()
        if not token:
            raise SetupError("the GitHub CLI on this host is not logged in")
        user = await self._user(token)
        self.cfg.github.auth = "gh"
        self.cfg.github.gh_config_dir = None
        self._token = token
        self.identity = {**user, "kind": "gh", "warning": self._warning(user)}
        return self.identity

    async def use_token(self, token: str, *, claim: bool) -> dict[str, Any]:
        """Act as the token's account. On a new host the token also proves its owner."""
        token = token.strip()
        if not token:
            raise SetupError("paste a token")
        user = await (self.claim(token) if claim else self._user(token))
        self.cfg.github.auth = "token"
        self._token = token
        self._secrets[self.cfg.github.token_env] = token
        self.identity = {**user, "kind": "token", "warning": self._warning(user)}
        return self.identity

    async def gh_web_start(self) -> dict[str, str]:
        """Start a GitHub CLI login for the service; the page shows the code."""
        return await self.gh_login.start()

    async def gh_web_poll(self, *, claim: bool) -> dict[str, Any]:
        """Whether the person has entered the code; once they have, the account they
        authorized becomes the service's (and, on a new host, the setup's owner)."""
        state = self.gh_login.state()
        if state in ("idle", "pending"):
            return {"state": state}
        if state == "failed":
            return {"state": "failed", "error": " ".join(self.gh_login.output[-2:])}
        token = await self.gh_login.token()
        if token is None:
            return {
                "state": "failed",
                "error": "the GitHub CLI has no token after the login",
            }
        try:
            user = await (self.claim(token) if claim else self._user(token))
        except SetupError as e:
            shutil.rmtree(self.gh_login.config_dir, ignore_errors=True)
            self.gh_login.proc = None
            return {"state": "failed", "error": str(e)}
        self.cfg.github.auth = "gh"
        self.cfg.github.gh_config_dir = str(self.gh_login.config_dir)
        self._token = token
        self.identity = {**user, "kind": "gh", "warning": self._warning(user)}
        return {"state": "done", "identity": self.identity}

    def token_url(self) -> str:
        """GitHub's page for a new classic token with the scopes the service needs."""
        q = urlencode({"scopes": "repo,read:org", "description": "auto-maintainer"})
        return f"{self.cfg.github.web_url}/settings/tokens/new?{q}"

    def app_manifest(
        self, base_url: str, purpose: Literal["identity", "signin"], org: str | None
    ) -> dict[str, Any]:
        """A GitHub App manifest and where to post it; GitHub sends the browser back to
        ``/setup/app/callback`` with a code that ``complete_app`` exchanges."""
        base = base_url.rstrip("/")
        self.cfg.base_url = base
        state = secrets.token_urlsafe(16)
        self._manifest_state[state] = purpose
        name = f"auto-maintainer {socket.gethostname()}"[:34]
        manifest: dict[str, Any] = {
            "name": name if purpose == "identity" else f"{name} sign-in"[:34],
            "url": base,
            "redirect_url": f"{base}/setup/app/callback",
            "callback_urls": [f"{base}/auth/callback", f"{base}/setup/signin/callback"],
            "public": False,
            "hook_attributes": {"url": f"{base}/webhooks/github", "active": False},
            # A sign-in app still needs to post reviews: people may post the drafts
            # they approve as themselves through it.
            "default_permissions": APP_PERMISSIONS
            if purpose == "identity"
            else {"pull_requests": "write", "metadata": "read"},
            "default_events": [],
        }
        owner = f"organizations/{org}/" if org else ""
        return {
            "url": f"{self.cfg.github.web_url}/{owner}settings/apps/new?state={state}",
            "manifest": json.dumps(manifest),
        }

    async def complete_app(self, code: str, state: str) -> dict[str, Any]:
        purpose = self._manifest_state.pop(state, None)
        if purpose is None:
            raise SetupError("this GitHub App answer does not belong to this setup")
        async with self._client() as client:
            r = await client.post(f"/app-manifests/{code}/conversions")
        if r.status_code not in (200, 201):
            raise SetupError(f"GitHub did not create the app ({r.status_code})")
        app = r.json()
        gh = self.cfg.github
        gh.oauth_client_id = app["client_id"]
        self._secrets[gh.oauth_client_secret_env] = app["client_secret"]
        if app.get("webhook_secret"):
            self._secrets[gh.webhook_secret_env] = app["webhook_secret"]
        self.signin = "app"
        slug = app.get("slug")
        self.app = {
            "slug": slug,
            "owner": (app.get("owner") or {}).get("login"),
            "install_url": f"{gh.web_url}/apps/{slug}/installations/new",
            "purpose": purpose,
        }
        if purpose == "identity":
            gh.auth = "app"
            gh.app_id = int(app["id"])
            gh.app_slug = slug
            self._pem = app["pem"]
            self.identity = {
                "login": f"{slug}[bot]",
                "name": app.get("name") or slug,
                "avatar_url": "",
                "kind": "app",
                "warning": "",
            }
        return self.app

    def signin_url(self, redirect_uri: str, state: str) -> str:
        """GitHub's authorize page for the app this setup created."""
        client_id = self.cfg.github.oauth_client_id
        if not client_id:
            raise SetupError("create the GitHub App first")
        q = urlencode(
            {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
        )
        return f"{self.cfg.github.web_url}/login/oauth/authorize?{q}"

    async def signin_token(self, code: str, redirect_uri: str) -> str:
        """Exchange the code GitHub sent back for the person's token."""
        gh = self.cfg.github
        secret = self._secrets.get(gh.oauth_client_secret_env)
        if not gh.oauth_client_id or not secret:
            raise SetupError("create the GitHub App first")
        async with self._client(gh.web_url) as client:
            r = await client.post(
                "/login/oauth/access_token",
                data={
                    "client_id": gh.oauth_client_id,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            raise SetupError("GitHub did not sign you in; try again")
        return str(token)

    # -- memory --------------------------------------------------------------------------

    def new_repo_url(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        q = urlencode(
            {
                "owner": owner,
                "name": name,
                "visibility": "private",
                "description": "Memory of auto-maintainer",
            }
        )
        return f"{self.cfg.github.web_url}/new?{q}"

    async def _app_token(self, repo: str) -> str | None:
        """An installation token for ``repo`` from the app this setup created, or
        ``None`` when the app is not installed there."""
        gh = self.cfg.github
        if self._pem is None or gh.app_id is None:
            return None
        now = int(time.time())
        assertion = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(gh.app_id)},
            self._pem,
            algorithm="RS256",
        )
        auth = {"Authorization": f"Bearer {assertion}"}
        async with self._client() as client:
            r = await client.get(f"/repos/{repo}/installation", headers=auth)
            if r.status_code != 200:
                return None
            t = await client.post(
                f"/app/installations/{r.json()['id']}/access_tokens", headers=auth
            )
        return t.json().get("token") if t.status_code == 201 else None

    async def _service_token(self, repo: str) -> str | None:
        """The credential the service itself will use for ``repo``: an installation
        token of its GitHub App, its token, or its GitHub CLI login (the one made in the
        browser, else the host's)."""
        gh = self.cfg.github
        if gh.auth == "app":
            if self._pem is None and gh.private_key_path:
                key = Path(gh.private_key_path).expanduser()
                if key.is_file():
                    self._pem = key.read_text(encoding="utf-8")
            return await self._app_token(repo)
        if self._token:
            return self._token
        if gh.auth == "token":
            return os.environ.get(gh.token_env) or None
        if gh.gh_config_dir or self.gh_login.state() != "none":
            token = await self.gh_login.token()
            if token:
                return token
        return await self.gh_token()

    async def memory(
        self, repo: str | None, create: bool, person_token: str | None = None
    ) -> dict[str, Any]:
        """Use ``repo`` as the memory repository, creating it private when asked;
        with ``repo`` empty, memory stays on this host.

        The repository is looked up with each credential at hand in turn (the
        signed-in maintainer's, the service's own, the host's GitHub CLI), because a
        private repository is invisible to a credential not given access to it: a
        GitHub App's sign-in sees only repositories the app is installed on, and a
        fine-grained token only those chosen for it. It is created with the
        maintainer's account. The service's own account must then reach it, since it
        keeps the memory there; that is checked last.
        """
        if not repo:
            self.cfg.memory.remote = None
            return {"repo": None}
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise SetupError("name the repository as owner/name")
        person = person_token or self._claimer_token
        service = await self._service_token(repo)
        host = await self.gh_token()
        candidates = [t for t in dict.fromkeys([person, service, host]) if t]
        if not candidates:
            raise SetupError("sign in with GitHub first")
        owner, name = repo.split("/", 1)
        tried: list[str] = []
        found: dict[str, Any] | None = None
        async with self._client() as client:
            for token in candidates:
                auth = {"Authorization": f"Bearer {token}"}
                r = await client.get(f"/repos/{repo}", headers=auth)
                if r.status_code == 200:
                    found = r.json()
                    break
                if r.status_code not in (401, 403, 404):
                    raise SetupError(f"GitHub answered {r.status_code} for {repo}")
                tried.append(await self._login_of(client, auth))
            if found is not None:
                if not found.get("private"):
                    raise SetupError(
                        f"{repo} is public; the memory repository must be private"
                    )
                created = False
            elif create:
                auth = {"Authorization": f"Bearer {person or candidates[0]}"}
                me = (await client.get("/user", headers=auth)).json().get("login")
                path = "/user/repos" if owner == me else f"/orgs/{owner}/repos"
                body = {
                    "name": name,
                    "private": True,
                    "auto_init": True,
                    "description": "Memory of auto-maintainer: profiles, knowledge, journals",
                }
                c = await client.post(path, headers=auth, json=body)
                if c.status_code not in (200, 201):
                    raise SetupError(
                        f"GitHub did not create {repo} ({c.status_code}): "
                        f"{c.json().get('message', '')}"
                    )
                created = True
            else:
                accounts = ", ".join(dict.fromkeys(tried)) or "no account"
                install = (
                    f" Install the app on it: {self._install_url()}"
                    if self.cfg.github.auth == "app"
                    else ""
                )
                raise SetupError(
                    f"{repo} does not exist, or none of these can see it: {accounts}. "
                    "A private repository is visible only to accounts given access; "
                    "a GitHub App's sign-in sees only repositories the app is installed "
                    "on, and a fine-grained token only the repositories chosen for it."
                    + install
                )
            if self.cfg.github.auth == "app":
                if await self._app_token(repo) is None:
                    self.cfg.memory.remote = repo
                    return {
                        "repo": repo,
                        "created": created,
                        "install": self._install_url(),
                        "warning": f"install the app on {repo} so the service can write there",
                    }
            elif (
                service is not None
                and (
                    await client.get(
                        f"/repos/{repo}", headers={"Authorization": f"Bearer {service}"}
                    )
                ).status_code
                != 200
            ):
                # Only a credential the service has, refused by GitHub, shows that the
                # service cannot reach it; without one there is nothing to check.
                who = (self.identity or {}).get("login") or await self._login_of(
                    client, {"Authorization": f"Bearer {service}"}
                )
                raise SetupError(
                    f"{repo} is there, but the service's account {who} cannot see it, "
                    "and the service keeps its memory with that account. Give it write "
                    "access to the repository, or use an account that has it."
                )
        self.cfg.memory.remote = repo
        return {"repo": repo, "created": created}

    async def _login_of(self, client: httpx.AsyncClient, auth: dict[str, str]) -> str:
        """The account a credential belongs to, for messages; ``?`` when GitHub will
        not say (an installation token has no user)."""
        r = await client.get("/user", headers=auth)
        if r.status_code == 200:
            return str(r.json().get("login") or "?")
        return "the service's GitHub App" if self.cfg.github.auth == "app" else "?"

    def _install_url(self) -> str:
        return f"{self.cfg.github.web_url}/apps/{self.cfg.github.app_slug}/installations/new"

    # -- finishing -----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        gh = self.cfg.github
        return {
            "configured": is_configured(self.config_dir),
            "base_url": self.cfg.base_url,
            "identity": self.identity,
            "claimer": self.claimer,
            "app": self.app,
            "auth": gh.auth,
            "signin": self.signin,
            "memory": self.cfg.memory.remote,
            "code_repo": self.cfg.code_repo,
            "approve_permission": self.cfg.self_update.approve_permission,
            "token_url": self.token_url(),
        }

    def finish(self, base_url: str, code_repo: str | None) -> HostConfig:
        """Write ``local.yaml``, ``secrets.env``, and the app key; the setup code expires."""
        if self.identity is None and not is_configured(self.config_dir):
            raise SetupError("choose the service's GitHub account first")
        if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", base_url.strip()):
            raise SetupError("the address must look like http://host:7777")
        if code_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", code_repo):
            raise SetupError("name the service's repository as owner/name")
        self.cfg.base_url = base_url.strip().rstrip("/")
        self.cfg.code_repo = code_repo or None
        for person in (self.claimer, self.identity):
            login = person["login"] if person and person.get("kind") != "app" else None
            if login and login not in self.cfg.operators:
                self.cfg.operators.append(login)
        if self._pem is not None:
            key = self.config_dir / "github-app.pem"
            write_private_file(key, self._pem)
            self.cfg.github.private_key_path = str(key)
        for name, value in self._secrets.items():
            set_host_secret(name, value, self.config_dir)
        save_host_config(self.cfg, self.config_dir)
        self.code_path.unlink(missing_ok=True)
        if self.gh_login.state() == "pending":
            self.gh_login.cancel()
        return self.cfg
