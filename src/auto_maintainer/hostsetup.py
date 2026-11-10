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

"""Host setup in the browser (design 7.3): the address, the service's repository
(its owner's fork or copy of auto-maintainer) and with it the main forge, GitHub or
GitLab, the account the service acts as there through the forge's CLI, the memory
repository, and the first agent, written to ``local.yaml`` so nobody edits a file or
types a command. When the host has no CLI for its forge, the page installs it
(``ghcli.install``, ``glabcli.install``).

One person owns a deployment: an admin of the service's repository, who proves it
by signing in on its forge (design 2.3). On a new host anyone who reaches the page
may begin, but someone without the setup link that ``am serve`` prints may only
claim it with the repository this checkout came from; with the link, any repository
of theirs. The repository is checked before setup finishes: its owner is an admin
there, it holds the code this host runs, and the service's account can push to it,
so self-improvement works later. After setup, the same steps run from the dashboard
for the owner, and the setup link (``am setup``) lets an admin of the service's
repository take ownership when its owner cannot.
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

from . import ghcli, subproc
from .config import (
    CONFIG_DIR,
    FORGE_DEFAULTS,
    LEGACY_SECRETS,
    ForgeConfig,
    HostConfig,
    is_configured,
    legacy_github,
    load_host_config,
    remove_host_secret,
    save_host_config,
    write_private_file,
)
from .domain.permissions import Permission

AGENT_BINARIES = {
    "claude-code": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "opencode": "opencode",
    "gemini": "gemini",
}


class SetupError(ValueError):
    """A setup step cannot be done; the message says why in the user's terms."""


async def _run_gh(args: list[str]) -> tuple[int, str]:
    binary = shutil.which("gh")
    if binary is None:
        return 127, ""
    result = await subproc.run([binary, *args], timeout=20)
    return result.exit_code, result.stdout.strip()


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


class GlabLogin:
    """``glab auth login --device`` run for a browser: GitLab's OAuth device flow, in
    which the person enters a one-time code on the GitLab site (GitLab 17.9 or later;
    a self-managed instance also needs an OAuth application, whose ``client_id`` is
    set for its host first). The login lands in ``config_dir`` (``GLAB_CONFIG_DIR``),
    never in the host user's own GitLab CLI configuration. ``throwaway`` is for a
    person signing in to the dashboard: ``discard`` deletes the folder afterwards.

    ``with_token`` signs in with a personal access token instead, for instances
    without the device flow; the token goes in on standard input.
    """

    CODE = re.compile(r"one-time code:\s*([A-Za-z0-9-]{4,})")
    URL = re.compile(r"(https://\S+/oauth/device)")

    def __init__(
        self,
        config_dir: Path,
        web_url: str = "https://gitlab.com",
        *,
        throwaway: bool = False,
        client_id: str | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.throwaway = throwaway
        self.client_id = client_id
        self.host = web_url.split("://", 1)[-1].split("/", 1)[0]
        self.proc: asyncio.subprocess.Process | None = None
        self.output: list[str] = []
        self._done: asyncio.Task[None] | None = None

    def env(self) -> dict[str, str]:
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITLAB_")}
        return {
            **env,
            "GLAB_CONFIG_DIR": str(self.config_dir),
            "GIT_CONFIG_GLOBAL": str(self.config_dir / "gitconfig"),
            "BROWSER": "true",
            "NO_COLOR": "1",
        }

    async def _client_id(self, binary: str) -> None:
        if self.client_id:
            await subproc.run(
                [
                    binary,
                    "config",
                    "set",
                    "client_id",
                    self.client_id,
                    "--host",
                    self.host,
                ],
                env=self.env(),
                timeout=20,
            )

    async def start(self, timeout: float = 30) -> dict[str, str]:
        """Start the device flow and return the code and the page to enter it on."""
        binary = shutil.which("glab")
        if binary is None:
            raise SetupError("the GitLab CLI is not installed on this host")
        self.cancel()
        self.output = []
        await self._client_id(binary)
        self.proc = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "login",
            "--hostname",
            self.host,
            "--device",
            "--git-protocol",
            "https",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env(),
            start_new_session=True,
        )
        assert self.proc.stdout
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
                "the GitLab CLI did not start a device login (GitLab 17.9 or later, "
                "and on a self-managed instance an OAuth application's client id, are "
                "needed; or sign in with a token): " + " ".join(self.output[-3:])
            )
        self._done = asyncio.create_task(self._drain())
        return {"code": code, "url": url}

    async def with_token(self, token: str) -> None:
        """Sign in with a personal access token, given on standard input."""
        binary = shutil.which("glab")
        if binary is None:
            raise SetupError("the GitLab CLI is not installed on this host")
        result = await subproc.run(
            [
                binary,
                "auth",
                "login",
                "--hostname",
                self.host,
                "--stdin",
                "--git-protocol",
                "https",
            ],
            env=self.env(),
            stdin=token.strip() + "\n",
            timeout=60,
        )
        if not result.ok:
            raise SetupError("the GitLab CLI did not accept the token")

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
        binary = shutil.which("glab")
        if binary is None:
            return None
        result = await subproc.run(
            [binary, "config", "get", "token", "--host", self.host],
            env=self.env(),
            timeout=20,
        )
        return result.stdout.strip() if result.ok and result.stdout.strip() else None

    def discard(self) -> None:
        """Stop the login and delete everything it wrote."""
        self.cancel()
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def cancel(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
        if self._done is not None:
            self._done.cancel()
        self.proc = None


def cli_login(
    kind: str,
    config_dir: Path,
    web_url: str,
    *,
    throwaway: bool = False,
    client_id: str | None = None,
) -> GhLogin | GlabLogin:
    """The forge CLI's sign-in for a forge of ``kind`` (design 5.16)."""
    if kind == "gitlab":
        return GlabLogin(config_dir, web_url, throwaway=throwaway, client_id=client_id)
    return GhLogin(config_dir, web_url, throwaway=throwaway)


FORGE_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_WEB = re.compile(r"^https?://[^\s/]+(/\S*)?$")


class ForgeRefused(SetupError):
    """A forge that cannot be added: ``status`` 409 when its id is taken, else 422."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class ForgeSteps:
    """The host setup's steps behind the Settings page, whose routes check who may:
    adding a forge (``web/forge_setup.py``; the service signs in there into its own
    folder for that forge's CLI under ``config_dir``, never the host user's), and the
    service's repository and its owner (``web/ownership.py``)."""

    def __init__(
        self, config_dir: Path, data_dir: Path, checkout: Path | None = None
    ) -> None:
        self.config_dir = config_dir
        self.data_dir = data_dir
        self.checkout = checkout or Path(__file__).resolve().parents[2]

    def forges(self) -> list[dict[str, str]]:
        """The forges in ``local.yaml``, the default first."""
        return [
            {"id": f.id, "kind": f.kind, "web_url": f.web}
            for f in load_host_config(self.config_dir).forges
        ]

    def cli_folder(self, forge_id: str, kind: str, *, default: bool = False) -> Path:
        """The service's own folder for a forge's CLI: ``gh`` for the default forge
        (the setup page's login), else one per forge."""
        if kind == "github":
            return self.config_dir / ("gh" if default else f"gh-{forge_id}")
        return self.config_dir / "glab" / forge_id

    def taken(self, forge_id: str) -> None:
        if any(f.id == forge_id for f in load_host_config(self.config_dir).forges):
            raise ForgeRefused(f"there is a forge {forge_id!r} already", 409)

    def new(
        self,
        forge_id: str,
        kind: str,
        web_url: str = "",
        api_url: str = "",
        oauth_client_id: str = "",
    ) -> ForgeConfig:
        """A forge not yet in ``local.yaml``, with its own CLI folder.

        Raises:
            ForgeRefused: The id is malformed or taken, or an address is not one.
        """
        if not FORGE_ID.match(forge_id):
            raise ForgeRefused(
                "the id is lowercase letters, digits, and dashes, starting with a letter"
            )
        self.taken(forge_id)
        web = web_url.strip().rstrip("/")
        api = api_url.strip().rstrip("/")
        for url in (web, api):
            if url and not _WEB.match(url):
                raise ForgeRefused(f"{url!r} is not a web address")
        try:
            return ForgeConfig(
                id=forge_id,
                kind=kind,  # type: ignore[arg-type]
                web_url=web,
                api_url=api,
                oauth_client_id=oauth_client_id.strip() or None,
                cli_config_dir=str(self.cli_folder(forge_id, kind)),
            )
        except ValueError as e:
            raise ForgeRefused(str(e)) from e

    async def install_cli(self, forge: ForgeConfig) -> None:
        """Install the forge's CLI into the data folder when the host has none.

        Raises:
            SetupError: It could not be installed.
        """
        binary = "glab" if forge.kind == "gitlab" else "gh"
        if shutil.which(binary) is not None:
            return
        try:
            if forge.kind == "gitlab":
                from . import glabcli

                await glabcli.install(self.data_dir)
            else:
                await ghcli.install(self.data_dir)
        except Exception as e:
            raise SetupError(str(e)) from e

    def login(self, forge: ForgeConfig) -> GhLogin | GlabLogin:
        """The forge CLI's sign-in into the forge's own folder."""
        return cli_login(
            forge.kind,
            Path(forge.cli_config_dir or ""),
            forge.web,
            client_id=forge.oauth_client_id,
        )

    async def account(self, forge: ForgeConfig, token: str) -> str:
        """The login ``token`` acts as on ``forge``, as the forge spells it.

        Raises:
            SetupError: The forge did not accept the token.
        """
        from .forges.registry import kind

        who = kind(forge.kind).who
        assert who is not None
        try:
            return (await who(forge.api, token))["login"]
        except Exception as e:
            raise SetupError(f"{forge.web} did not accept the sign-in") from e

    def with_forge(self, forge: ForgeConfig) -> HostConfig:
        """The host's setup as it is now, with ``forge`` added last.

        Raises:
            ForgeRefused: Its id was taken meanwhile.
        """
        self.taken(forge.id)
        current = load_host_config(self.config_dir)
        return HostConfig.model_validate(
            {
                **current.model_dump(),
                "forges": [*current.model_dump()["forges"], forge.model_dump()],
            }
        )

    def keep(self, cfg: HostConfig) -> None:
        save_host_config(cfg, self.config_dir)

    # -- the service's repository and its owner -----------------------------------------

    def locate(self, link: str) -> tuple[ForgeConfig, str]:
        """The forge a repository link is on, and the repository's name as the
        service names it (``group/name`` on the main forge, ``<forge>:group/name``
        elsewhere).

        Raises:
            ForgeRefused: Not a repository's link, or on a forge this host does not
                serve.
        """
        parsed = parse_link(link)
        if parsed is None:
            raise ForgeRefused(
                "paste the repository's link, like https://github.com/you/auto-maintainer"
            )
        web, path = parsed
        for i, forge in enumerate(load_host_config(self.config_dir).forges):
            if forge.web.lower() == web.lower():
                return forge, path if i == 0 else f"{forge.id}:{path}"
        raise ForgeRefused(
            f"this service does not serve {web}; add it under Forges first"
        )

    async def _service_token(self, forge: ForgeConfig) -> str | None:
        if forge.kind == "gitlab":
            from .forges.gitlab.client import GlabToken

            return await GlabToken(forge.host, forge.cli_config_dir).token(None)
        from .forges.github.auth import StaticTokenProvider

        return await StaticTokenProvider(
            None, gh_config_dir=forge.cli_config_dir
        ).token(None)

    async def check_repo(
        self, forge: ForgeConfig, path: str
    ) -> tuple[list[dict[str, Any]], str, str]:
        """Check a repository as the service's repository, with the service's own
        account there: it holds the code this host runs, and the service can push
        to it. Returns the checks, its path as the forge spells it, and its default
        branch."""
        from .forges.registry import kind

        account = kind(forge.kind).account
        token = await self._service_token(forge)
        access = await account.repo(forge.api, token, path) if token else None
        if access is None:
            return (
                [
                    {
                        "id": "repo",
                        "ok": False,
                        "text": f"{path} does not exist, or the service's account cannot see it",
                    }
                ],
                path,
                "main",
            )
        assert token is not None
        head = await subproc.run(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], timeout=10
        )
        sha = head.stdout.strip() if head.exit_code == 0 else ""
        checks = []
        if sha:
            holds = await account.has_commit(forge.api, token, access.path, sha)
            checks.append(
                {
                    "id": "code",
                    "ok": holds,
                    "text": f"it holds the code this host runs ({sha[:10]})"
                    if holds
                    else f"it does not hold the code this host runs ({sha[:10]})",
                }
            )
        push = access.permission.at_least(Permission.WRITE)
        checks.append(
            {
                "id": "push",
                "ok": push,
                "text": "the service's account can push to it"
                if push
                else "the service's account cannot push to it; give it write access",
            }
        )
        return checks, access.path, access.default_branch

    def set_owner(self, owner: str) -> HostConfig:
        """Name the owner in ``local.yaml``."""
        cfg = load_host_config(self.config_dir)
        cfg.owner = owner
        save_host_config(cfg, self.config_dir)
        return cfg

    def with_repo(self, name: str, owner: str, branch: str) -> HostConfig:
        """The host's setup as it is now, with another service repository and its
        owner (not yet written)."""
        cfg = load_host_config(self.config_dir)
        cfg.code_repo, cfg.owner = name, owner
        cfg.self_update.branch = branch
        return HostConfig.model_validate(cfg.model_dump())


class HostSetup:
    """One setup in progress. Choices stay in memory until ``finish`` writes them.

    The main forge is where the service's repository lives (GitHub or GitLab); the
    service acts there as the account signed in through that forge's CLI, kept in its
    own folder under ``config_dir``. Whoever signs in as the owner must be an admin of
    the service's repository; they own the deployment once setup finishes.

    Args:
        config_dir: Where ``local.yaml`` and the CLI logins go.
        checkout: The service's own git checkout (its ``origin`` and running commit).
        host_token: The token of the forge CLI already logged in on this host, for
            ``(kind, web_url)``; ``None`` when it is not logged in.
        login_factory: Makes the forge CLI's sign-in (``cli_login``).
    """

    def __init__(
        self,
        config_dir: Path = CONFIG_DIR,
        *,
        checkout: Path | None = None,
        host_token: Callable[[str, str], Awaitable[str | None]] | None = None,
        login_factory: Callable[..., Any] = cli_login,
    ) -> None:
        self.config_dir = config_dir
        self.checkout = checkout or Path(__file__).resolve().parents[2]
        self.host_token = host_token or _host_cli_token
        self.login_factory = login_factory
        self.cfg = (
            load_host_config(config_dir) if is_configured(config_dir) else HostConfig()
        )
        self.configured_repo = self.cfg.code_repo
        self.identity: dict[str, Any] | None = None
        """The account the service acts as, once chosen in this setup."""
        self.claimer: dict[str, Any] | None = None
        """The admin of the service's repository who signed in as its owner."""
        self.checks: list[dict[str, Any]] = []
        """The last verification of the service's repository (``verify``)."""
        self.reconnect = (
            legacy_github(config_dir) if is_configured(config_dir) else None
        )
        """How the host acted on GitHub before only the GitHub CLI remained (``"a
        GitHub App"``, ``"a token"``); such a host must connect the GitHub CLI."""
        self._service_token: str | None = None
        self._owner_token: str | None = None
        self._candidate: tuple[dict[str, Any], str] | None = None
        """Who signed in as the owner but is not yet an admin of the repository named:
        they own it once they are (``_try_claim``), such as after making a copy."""
        self._login: Any = None
        self.signing: str | None = None
        """Whom the sign-in in progress is for: ``owner`` or ``service``."""
        self._checkout: dict[str, str] | None | Literal[False] = False

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

    # -- the main forge and the service's repository -------------------------------------

    @property
    def forge(self) -> ForgeConfig:
        """The main forge: where the service's repository lives."""
        return self.cfg.forges[0]

    @property
    def folder(self) -> Path:
        """The service's own login folder for the main forge's CLI."""
        return self.config_dir / (
            "gh" if self.forge.kind == "github" else f"glab/{self.forge.id}"
        )

    def _kind(self) -> Any:
        from .forges.registry import kind

        return kind(self.forge.kind)

    async def checkout_repo(self) -> dict[str, str] | None:
        """Where this checkout came from (its ``origin``): ``web_url``, ``path``,
        ``kind`` when the host says it, and ``link``."""
        if self._checkout is not False:
            return self._checkout
        found = None
        if (self.checkout / ".git").exists():
            result = await subproc.run(
                ["git", "-C", str(self.checkout), "remote", "get-url", "origin"],
                timeout=10,
            )
            parsed = parse_link(result.stdout) if result.exit_code == 0 else None
            if parsed:
                web, path = parsed
                found = {"web_url": web, "path": path, "link": f"{web}/{path}"}
                if kind := kind_of_host(web):
                    found["kind"] = kind
        self._checkout = found
        return found

    async def head(self) -> str | None:
        """The commit this host runs."""
        result = await subproc.run(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], timeout=10
        )
        return result.stdout.strip() if result.exit_code == 0 else None

    def repo_link(self) -> str | None:
        return f"{self.forge.web}/{self.cfg.code_repo}" if self.cfg.code_repo else None

    async def is_checkout_repo(self) -> bool:
        """Whether the service's repository is the one this checkout came from: only
        then may someone set a new host up without the host's setup link."""
        mine = await self.checkout_repo()
        return bool(
            mine
            and self.cfg.code_repo
            and mine["web_url"].lower() == self.forge.web.lower()
            and mine["path"].lower() == self.cfg.code_repo.lower()
        )

    async def set_repo(
        self, link: str, kind: str | None = None, oauth_client_id: str | None = None
    ) -> dict[str, Any]:
        """Name the service's repository by its link. On a new host its forge
        becomes the main forge; later it must be on the main forge (another forge's
        repository is chosen from the Settings page).

        Raises:
            SetupError: The link is not a repository's, or names another forge.
        """
        parsed = parse_link(link)
        if parsed is None:
            raise SetupError(
                "paste the repository's link, like https://github.com/you/auto-maintainer"
            )
        web, path = parsed
        kind = kind or kind_of_host(web)
        if kind not in ("github", "gitlab"):
            raise SetupError(f"say whether {web} is GitHub or GitLab")
        same = web.lower() == self.forge.web.lower() and kind == self.forge.kind
        if not same:
            if is_configured(self.config_dir):
                raise SetupError(
                    f"this host's main forge is {self.forge.web}; choose a repository "
                    "on another forge from the Settings page (Owner)"
                )
            public = FORGE_DEFAULTS[kind]["web_url"]
            self.cfg.forges[0] = ForgeConfig(
                id=kind,
                kind=kind,  # type: ignore[arg-type]
                web_url="" if web == public else web,
                oauth_client_id=oauth_client_id or None,
            )
            self.identity = self.claimer = self._candidate = None
            self._service_token = self._owner_token = None
        elif oauth_client_id is not None:
            self.forge.oauth_client_id = oauth_client_id or None
        self.cfg.code_repo = path
        self.checks = []
        if self.identity or self.claimer:
            await self._try_claim()
            self.checks = await self.verify()
        return {"repo": path, "forge": self._forge_view()}

    async def _try_claim(self) -> None:
        """Make the person who signed in as the owner the owner, once they are an
        admin of the repository named."""
        if self._candidate is None or not self.cfg.code_repo:
            return
        person, token = self._candidate
        access = await self._access(token)
        if access is not None and access.permission is Permission.ADMIN:
            self.claimer, self._owner_token = person, token
            self._candidate = None

    def _forge_view(self) -> dict[str, Any]:
        return {
            "id": self.forge.id,
            "kind": self.forge.kind,
            "web_url": self.forge.web,
            "device": self.forge.kind == "github"
            or self.forge.web == FORGE_DEFAULTS["gitlab"]["web_url"]
            or bool(self.forge.oauth_client_id),
        }

    # -- the forge's CLI -----------------------------------------------------------------

    def cli_name(self) -> str:
        return "glab" if self.forge.kind == "gitlab" else "gh"

    async def install_cli(self) -> dict[str, Any]:
        """Install the main forge's CLI into the data folder."""
        try:
            if self.forge.kind == "gitlab":
                from . import glabcli

                path = await glabcli.install(self.cfg.data_path)
            else:
                path = await ghcli.install(self.cfg.data_path)
        except Exception as e:
            raise SetupError(str(e)) from e
        return {"installed": True, "path": path}

    async def _who(self, token: str) -> dict[str, Any]:
        who = self._kind().who
        try:
            return dict(await who(self.forge.api, token))
        except Exception as e:
            raise SetupError(f"{self.forge.web} did not accept this sign-in") from e

    async def detect(self, *, private: bool) -> dict[str, Any]:
        """What this host has. ``private`` adds the account the forge's CLI on the
        host is logged in to, which only the owner of the setup may see."""
        cli: dict[str, Any] = {
            "name": self.cli_name(),
            "installed": shutil.which(self.cli_name()) is not None,
        }
        if private:
            token = await self.host_token(self.forge.kind, self.forge.web)
            if token:
                try:
                    cli["user"] = await self._who(token)
                except SetupError as e:
                    cli["error"] = str(e)
        return {
            "cli": cli,
            "agents": {
                name: bool(shutil.which(b)) for name, b in AGENT_BINARIES.items()
            },
            "checkout": await self.checkout_repo(),
            "hostname": socket.gethostname(),
        }

    # -- signing in ----------------------------------------------------------------------

    def _new_login(self) -> Any:
        staging = self.config_dir / "setup-signin"
        if self._login is not None:
            self._login.cancel()
        shutil.rmtree(staging, ignore_errors=True)
        self._login = self.login_factory(
            self.forge.kind,
            staging,
            self.forge.web,
            client_id=self.forge.oauth_client_id,
        )
        return self._login

    def cancel(self) -> None:
        """Stop a sign-in still waiting, as when the server stops; what it wrote stays
        in its staging folder until the next sign-in clears it."""
        if self._login is not None:
            self._login.cancel()

    async def signin_start(self, role: str) -> dict[str, str]:
        """Start the forge CLI's one-time code: ``owner`` for the admin of the
        service's repository (whose login the service then acts as too), ``service``
        for another account the service acts as."""
        self.signing = role
        return await self._new_login().start()

    async def signin_poll(
        self, *, may_claim: bool = True, trusted: bool = False
    ) -> dict[str, Any]:
        """Whether the person has entered the code; once they have, check the
        account and keep its login as the service's."""
        login = self._login
        if login is None:
            return {"state": "idle"}
        state = login.state()
        if state in ("idle", "pending"):
            return {"state": state}
        if state == "failed":
            output = " ".join(getattr(login, "output", [])[-2:])
            login.discard()
            return {"state": "failed", "error": output or "the sign-in did not finish"}
        token = await login.token()
        if not token:
            login.discard()
            return {"state": "failed", "error": "the forge's CLI holds no sign-in"}
        try:
            person = await self._signed_in(
                token, self.signing or "owner", may_claim=may_claim, trusted=trusted
            )
        except SetupError as e:
            login.discard()
            return {"state": "failed", "error": str(e)}
        return {"state": "done", "person": person}

    async def signin_token(
        self, role: str, token: str, *, may_claim: bool = True, trusted: bool = False
    ) -> dict[str, Any]:
        """Sign in with a personal access token instead (GitLab)."""
        if self.forge.kind != "gitlab":
            raise SetupError("GitHub signs in with the one-time code")
        self.signing = role
        login = self._new_login()
        await login.with_token(token)
        try:
            person = await self._signed_in(
                token.strip(), role, may_claim=may_claim, trusted=trusted
            )
        except SetupError:
            login.discard()
            raise
        return {"state": "done", "person": person}

    async def _signed_in(
        self, token: str, role: str, *, may_claim: bool, trusted: bool
    ) -> dict[str, Any]:
        """Check who signed in; an owner must be an admin of the service's
        repository. The login then replaces the service's own. ``trusted`` (the
        setup link, or the owner after setup) keeps the login of someone not yet an
        admin, who becomes the owner once they are, as after making a copy."""
        person = await self._who(token)
        owner = role != "owner"
        if role == "owner":
            if not self.cfg.code_repo:
                raise SetupError("name the service's repository first")
            if not may_claim:
                raise SetupError(
                    f"{self.cfg.code_repo} is not the repository this host's code came "
                    "from, so only the setup link printed on the host may set it up "
                    "with it"
                )
            access = await self._access(token)
            owner = access is not None and access.permission is Permission.ADMIN
            if not owner and not trusted:
                raise SetupError(
                    f"{person['login']} is not an admin of {self.cfg.code_repo}; its "
                    "admin owns this service"
                )
            if owner:
                self.claimer, self._owner_token = person, token
                self._candidate = None
            else:
                self._candidate = (person, token)
        self._keep_login()
        self.identity = person
        self._service_token = token
        self.checks = await self.verify()
        return {**person, "owner": owner}

    def _keep_login(self) -> None:
        """Move the finished sign-in into the service's own login folder."""
        assert self._login is not None
        staging = Path(self._login.config_dir)
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, self.folder)
        self._login = None
        self.forge.cli_config_dir = str(self.folder)

    async def use_host_login(self) -> dict[str, Any]:
        """Act as the forge CLI login already on this host."""
        token = await self.host_token(self.forge.kind, self.forge.web)
        if not token:
            raise SetupError(f"the {self.cli_name()} CLI on this host is not logged in")
        person = await self._who(token)
        self.forge.cli_config_dir = None
        self.identity = person
        self._service_token = token
        self.checks = await self.verify()
        return person

    async def _token_now(self) -> str | None:
        """The service's token: the login made in this setup, else the kept one,
        else the host's own CLI login. ``None`` for a host that must connect again."""
        if self._service_token:
            return self._service_token
        if self.reconnect:
            return None
        if self.forge.cli_config_dir:
            kept = self.login_factory(
                self.forge.kind,
                Path(self.forge.cli_config_dir),
                self.forge.web,
                client_id=self.forge.oauth_client_id,
            )
            token = await kept.token()
            if token:
                return token
        return await self.host_token(self.forge.kind, self.forge.web)

    # -- the service's repository ------------------------------------------------------

    async def _access(self, token: str, path: str | None = None) -> Any:
        account = self._kind().account
        try:
            return await account.repo(self.forge.api, token, path or self.cfg.code_repo)
        except Exception as e:
            raise SetupError(str(e)) from e

    async def verify(self) -> list[dict[str, Any]]:
        """Check the service's repository: its owner is an admin there, it holds
        the code this host runs, and the service's account can push to it, which
        self-improvement needs (design 5.12). Sets the branch deploys come from."""
        repo = self.cfg.code_repo
        if not repo:
            return []
        out: list[dict[str, Any]] = []
        service = await self._token_now()
        reader = self._owner_token or service
        if reader is None:
            return [{"id": "repo", "ok": False, "text": "sign in to check it"}]
        access = await self._access(reader)
        if access is None:
            return [
                {
                    "id": "repo",
                    "ok": False,
                    "text": f"{repo} does not exist, or the account signed in cannot see it",
                }
            ]
        self.cfg.code_repo = access.path
        self.cfg.self_update.branch = access.default_branch
        if self.claimer and self._owner_token:
            theirs = (
                access
                if reader == self._owner_token
                else await self._access(self._owner_token)
            )
            admin = theirs is not None and theirs.permission is Permission.ADMIN
            login = self.claimer["login"]
            out.append(
                {
                    "id": "owner",
                    "ok": admin,
                    "text": (
                        f"{login} is an admin of {access.path} and owns this service"
                        if admin
                        else f"{login} is not an admin of {access.path}; its admin owns "
                        "this service"
                    ),
                }
            )
        head = await self.head()
        if head:
            try:
                holds = await self._kind().account.has_commit(
                    self.forge.api, reader, access.path, head
                )
            except Exception as e:
                raise SetupError(str(e)) from e
            out.append(
                {
                    "id": "code",
                    "ok": holds,
                    "text": (
                        f"it holds the code this host runs ({head[:10]})"
                        if holds
                        else f"it does not hold the code this host runs ({head[:10]}); "
                        "push this checkout's branch to it, or fork the repository "
                        "this host came from"
                    ),
                }
            )
        mine = await self._access(service) if service else None
        push = bool(mine and mine.permission.at_least(Permission.WRITE))
        who = (self.identity or {}).get("login", "the service's account")
        out.append(
            {
                "id": "push",
                "ok": push,
                "text": (
                    f"{who} can push to it, so the service opens its own changes there"
                    if push
                    else f"{who} cannot push to {access.path}; give it write access, so "
                    "the service can open its own changes there"
                ),
            }
        )
        return out

    async def copy_upstream(self) -> dict[str, Any]:
        """Make the owner's own copy of the repository this host came from (a fork
        on the same forge, else an import), and name it the service's repository."""
        source = await self.checkout_repo()
        if source is None:
            raise SetupError("this checkout does not say where its code came from")
        token = (
            self._owner_token
            or (self._candidate[1] if self._candidate else None)
            or await self._token_now()
        )
        if not token:
            raise SetupError("sign in first")
        same = source["web_url"].lower() == self.forge.web.lower()
        try:
            made = await self._kind().account.copy(
                self.forge.api, token, source["link"], source["path"] if same else None
            )
        except Exception as e:
            raise SetupError(str(e)) from e
        self.cfg.code_repo = made.path
        await self._try_claim()
        self.checks = await self.verify()
        return {"repo": made.path}

    # -- memory --------------------------------------------------------------------------

    async def memory(
        self, repo: str | None, create: bool, person_token: str | None = None
    ) -> dict[str, Any]:
        """Use ``repo`` on the main forge as the memory repository, creating it
        private when asked; with ``repo`` empty, memory stays on this host.

        The repository is looked up with each credential at hand in turn (the
        signed-in person's, the owner's, the service's own, the host's CLI), because
        a private repository is invisible to an account not given access to it. It
        is created with the person's account. The service's own account must then
        be able to write to it, since it keeps the memory there.
        """
        if not repo:
            self.cfg.memory.remote = None
            return {"repo": None}
        if not re.fullmatch(r"[\w.-]+(/[\w.-]+)+", repo):
            raise SetupError("name the repository by its path, like you/am-memory")
        service = await self._token_now()
        host = await self.host_token(self.forge.kind, self.forge.web)
        person = person_token or self._owner_token
        candidates = [t for t in dict.fromkeys([person, service, host]) if t]
        if not candidates:
            raise SetupError("sign in first")
        account = self._kind().account
        tried: list[str] = []
        found = None
        for token in candidates:
            found = await self._access(token, repo)
            if found is not None:
                break
            with contextlib.suppress(SetupError):
                tried.append((await self._who(token))["login"])
        created = False
        if found is None and create:
            try:
                found = await account.create_private(
                    self.forge.api,
                    person or candidates[0],
                    repo,
                    "Memory of auto-maintainer: profiles, knowledge, journals",
                )
            except Exception as e:
                raise SetupError(str(e)) from e
            created = True
        if found is None:
            accounts = ", ".join(dict.fromkeys(tried)) or "no account"
            raise SetupError(
                f"{repo} does not exist, or none of these can see it: {accounts}. "
                "A private repository is visible only to accounts given access to it."
            )
        if not found.private:
            raise SetupError(f"{repo} is public; the memory repository must be private")
        if service is not None:
            mine = await self._access(service, found.path)
            if mine is None or not mine.permission.at_least(Permission.WRITE):
                who = (self.identity or {}).get("login") or "the service's account"
                raise SetupError(
                    f"{found.path} is there, but {who} cannot write to it, and the "
                    "service keeps its memory with that account. Give it write "
                    "access, or use an account that has it."
                )
        self.cfg.memory.remote = found.path
        return {"repo": found.path, "created": created}

    # -- finishing -----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "configured": is_configured(self.config_dir),
            "base_url": self.cfg.base_url,
            "forge": self._forge_view(),
            "repo": self.cfg.code_repo,
            "repo_link": self.repo_link(),
            "owner": self.cfg.owner,
            "identity": self.identity,
            "claimer": self.claimer,
            "candidate": self._candidate[0] if self._candidate else None,
            "checks": self.checks,
            "reconnect": self.reconnect,
            "memory": self.cfg.memory.remote,
            "default_agent": self.cfg.default_agent,
        }

    def finish(self, base_url: str, default_agent: str | None = None) -> HostConfig:
        """Write ``local.yaml``; the setup code expires. A new host, or a changed
        service repository, needs an owner who signed in as an admin of it and a
        repository that passed every check. A host that must connect again loses
        what its GitHub App or token setup kept: their secrets and the app's key."""
        new = not is_configured(self.config_dir)
        if self.identity is None and (self.reconnect or new):
            raise SetupError("sign the service in first")
        if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", base_url.strip()):
            raise SetupError(
                "the address must look like https://host or http://host:7777"
            )
        if default_agent and default_agent not in AGENT_BINARIES:
            raise SetupError(f"there is no agent named {default_agent!r}")
        changed = (self.cfg.code_repo or "").lower() != (
            self.configured_repo or ""
        ).lower()
        if new or changed or self.claimer:
            if not self.cfg.code_repo:
                raise SetupError("name the service's repository")
            if self.claimer is None:
                raise SetupError(
                    f"sign in as an admin of {self.cfg.code_repo}, who owns this service"
                )
            failed = [c["text"] for c in self.checks if not c["ok"]]
            if failed or not self.checks:
                raise SetupError(
                    "the service's repository does not pass its checks: "
                    + ("; ".join(failed) or "check it first")
                )
            self.cfg.owner = self.claimer["login"]
        self.cfg.base_url = base_url.strip().rstrip("/")
        self.cfg.default_agent = default_agent or None
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
        if self._login is not None:
            self._login.discard()
            self._login = None
        self.configured_repo = self.cfg.code_repo
        return self.cfg


KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}


def kind_of_host(web_url: str) -> str | None:
    """``github`` or ``gitlab`` for the public forges; ``None`` for others."""
    host = web_url.split("://", 1)[-1].split("/", 1)[0].lower()
    return KNOWN_HOSTS.get(host)


def parse_link(link: str) -> tuple[str, str] | None:
    """``(web_url, path)`` from a repository's web or git address (``https://host/
    group/name``, ``git@host:group/name.git``, ``ssh://git@host/group/name``),
    dropping any credentials and the ``.git`` suffix."""
    text = link.strip()
    if m := re.fullmatch(r"[\w.-]+@([\w.-]+):(.+?)(?:\.git)?/?", text):
        host, path, scheme = m.group(1), m.group(2), "https"
    elif m := re.fullmatch(
        r"(https?|ssh|git)://(?:[^@/]+@)?([\w.-]+(?::\d+)?)/(.+?)(?:\.git)?/?", text
    ):
        scheme = "http" if m.group(1) == "http" else "https"
        host, path = m.group(2), m.group(3)
        if m.group(1) in ("ssh", "git"):
            host = host.split(":", 1)[0]
    else:
        return None
    path = path.split("/-/", 1)[0].strip("/")
    if not re.fullmatch(r"[\w.-]+(/[\w.-]+)+", path):
        return None
    return f"{scheme}://{host}", path


async def _host_cli_token(kind: str, web_url: str) -> str | None:
    """The token of the forge CLI the host user is logged in with."""
    host = web_url.split("://", 1)[-1].split("/", 1)[0]
    if kind == "gitlab":
        from .forges.gitlab.client import GlabToken

        return await GlabToken(host).token(None)
    code, out = await _run_gh(["auth", "token", "--hostname", host])
    return out if code == 0 and out else None
