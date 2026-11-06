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

"""Host setup in the browser (design 7.3): the GitHub CLI login the service acts as,
the memory repository, and the address, written to ``local.yaml`` so nobody edits a
file or types a command. The service acts on GitHub only through the GitHub CLI; when
the host has none, the page installs it (``ghcli.install``).

Who may set up a new host is decided by GitHub, like every other configuration
change: the person must show that they maintain the service's own repository (the
checkout's ``origin``) by authorizing the GitHub CLI login on github.com. Until
someone has, anyone who reaches the page may start, and the first such maintainer
owns the setup. The one-time code that ``am serve`` prints also proves ownership, for
a checkout without a GitHub remote and for using the GitHub CLI's login already on
the host. After setup, the same steps run from the dashboard behind the service gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import shutil
import signal
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from . import ghcli, subproc
from .config import (
    CONFIG_DIR,
    LEGACY_SECRETS,
    HostConfig,
    is_configured,
    legacy_github,
    load_host_config,
    remove_host_secret,
    save_host_config,
    write_private_file,
)

AGENT_BINARIES = {
    "claude-code": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "opencode": "opencode",
    "gemini": "gemini",
}

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
        self.reconnect = (
            legacy_github(config_dir) if is_configured(config_dir) else None
        )
        """How the host acted on GitHub before only the GitHub CLI remained (``"a
        GitHub App"``, ``"a token"``); such a host must connect the GitHub CLI."""
        self._token: str | None = None
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

    async def install_gh(self) -> dict[str, Any]:
        """Install the GitHub CLI into the data folder (``ghcli.install``)."""
        try:
            path = await ghcli.install(self.cfg.data_path, transport=self.transport)
        except ghcli.InstallError as e:
            raise SetupError(str(e)) from e
        return {"installed": True, "path": path}

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
        self.cfg.github.gh_config_dir = None
        self._token = token
        self.identity = {**user, "kind": "gh", "warning": self._warning(user)}
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
        self.cfg.github.gh_config_dir = str(self.gh_login.config_dir)
        self._token = token
        self.identity = {**user, "kind": "gh", "warning": self._warning(user)}
        return {"state": "done", "identity": self.identity}

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

    async def _service_token(self) -> str | None:
        """The credential the service itself will use: the GitHub CLI login chosen in
        this setup, else the one made in the browser earlier, else the host's own.
        ``None`` for a host that must connect again, whose old account is gone."""
        if self._token:
            return self._token
        if self.reconnect:
            return None
        if self.cfg.github.gh_config_dir:
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
        private repository is invisible to an account not given access to it. It is
        created with the maintainer's account. The service's own account must then
        reach it, since it keeps the memory there; that is checked last.
        """
        if not repo:
            self.cfg.memory.remote = None
            return {"repo": None}
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise SetupError("name the repository as owner/name")
        person = person_token or self._claimer_token
        service = await self._service_token()
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
                raise SetupError(
                    f"{repo} does not exist, or none of these can see it: {accounts}. "
                    "A private repository is visible only to accounts given access "
                    "to it."
                )
            if (
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
        not say."""
        r = await client.get("/user", headers=auth)
        if r.status_code == 200:
            return str(r.json().get("login") or "?")
        return "?"

    # -- finishing -----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "configured": is_configured(self.config_dir),
            "base_url": self.cfg.base_url,
            "identity": self.identity,
            "claimer": self.claimer,
            "reconnect": self.reconnect,
            "memory": self.cfg.memory.remote,
            "code_repo": self.cfg.code_repo,
            "approve_permission": self.cfg.self_update.approve_permission,
        }

    def finish(self, base_url: str, code_repo: str | None) -> HostConfig:
        """Write ``local.yaml``; the setup code expires. A host that must connect again
        loses what its GitHub App or token setup kept: their secrets and the app's key."""
        if self.identity is None and (
            self.reconnect or not is_configured(self.config_dir)
        ):
            raise SetupError("connect the service's GitHub account first")
        if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", base_url.strip()):
            raise SetupError("the address must look like http://host:7777")
        if code_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", code_repo):
            raise SetupError("name the service's repository as owner/name")
        self.cfg.base_url = base_url.strip().rstrip("/")
        self.cfg.code_repo = code_repo or None
        for person in (self.claimer, self.identity):
            login = person["login"] if person else None
            if login and login not in self.cfg.operators:
                self.cfg.operators.append(login)
        save_host_config(self.cfg, self.config_dir)
        if self.reconnect:
            for name in LEGACY_SECRETS:
                remove_host_secret(name, self.config_dir)
            (self.config_dir / "github-app.pem").unlink(missing_ok=True)
            self.reconnect = None
        self.code_path.unlink(missing_ok=True)
        if self.gh_login.state() == "pending":
            self.gh_login.cancel()
        return self.cfg
