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

"""Installing an agent CLI on the service's host, which the owner of the service's
repository ask for on the dashboard (``web/settings.py``).

Each CLI comes from its publisher: from npm when the publisher ships it there
(Codex, Claude Code, DeepSeek Harness, pi, OpenCode, Gemini CLI), where npm checks every package against the
registry's integrity hash; otherwise from the publisher's latest GitHub release,
checked against the SHA-256 digest GitHub lists for the asset (Antigravity). Only
the packages and repositories in ``SOURCES`` can be installed, by these exact names.

A CLI goes into ``<data>/agents/<name>/``, and ``<data>/bin/<binary>`` links to it.
``<data>/bin`` comes first on the service's ``PATH``, so the runtime finds it at once.
In a container, an npm-installed CLI runs in place from that folder, read-only, with
the host's ``node`` (``agents/container.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..domain.agents import InstalledAgent
from ..subproc import run

NPM_TIMEOUT = 1200.0
"""Claude Code's package carries a native build of about 200 MB."""


class InstallError(RuntimeError):
    """The CLI could not be installed; the message says why."""


@dataclass(frozen=True)
class Source:
    """Where one agent CLI comes from."""

    binary: str
    npm: str | None = None
    """The npm package, preferred whenever the publisher ships one."""
    github: str | None = None
    """``owner/repo`` whose latest release carries the build."""
    asset: str = ""
    """The release asset's name; ``{arch}`` is ``x64`` or ``arm64``."""
    member: str = ""
    """The binary's name inside the release archive, when not ``binary``."""

    def describe(self) -> str:
        if self.npm:
            return f"npm {self.npm}"
        return f"github.com/{self.github} releases"


SOURCES: dict[str, Source] = {
    "codex": Source(binary="codex", npm="@openai/codex"),
    "claude-code": Source(binary="claude", npm="@anthropic-ai/claude-code"),
    "dsh": Source(binary="dsh", npm="@deepseek-ai/dsh"),
    "pi": Source(binary="pi", npm="@mariozechner/pi-coding-agent"),
    "opencode": Source(binary="opencode", npm="opencode-ai"),
    "gemini": Source(binary="gemini", npm="@google/gemini-cli"),
    "antigravity": Source(
        binary="agy",
        github="google-antigravity/antigravity-cli",
        asset="agy_cli_linux_{arch}.tar.gz",
        member="antigravity",
    ),
}
"""Every CLI the dashboard installs. Antigravity is not on npm: ``antigravity-cli``
there is somebody else's package."""

_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def installs_dir(data: Path) -> Path:
    """Where installed CLIs live; containers mount it read-only."""
    return data / "agents"


class AgentInstaller:
    """Installs the CLIs in ``SOURCES`` into ``<data>/agents``.

    Args:
        data: The service's data folder.
        npm: The npm command.
        transport: For tests: answers GitHub's requests.
    """

    def __init__(
        self,
        data: Path,
        *,
        npm: str = "npm",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.data = data
        self.npm = npm
        self.transport = transport
        self._locks: dict[str, asyncio.Lock] = {}

    def sources(self) -> dict[str, str]:
        """The CLIs this host can install, by runtime name, with where each comes
        from; one that needs npm is left out where npm is missing."""
        have_npm = shutil.which(self.npm) is not None
        return {
            name: source.describe()
            for name, source in SOURCES.items()
            if source.github or have_npm
        }

    async def install(
        self, name: str, on_line: Callable[[str], Awaitable[None]] | None = None
    ) -> InstalledAgent:
        """Install the latest release of ``name``, replacing an earlier install.

        Raises:
            InstallError: Unknown name, no npm, or the download failed or did not
                match its published digest.
        """
        source = SOURCES.get(name)
        if source is None:
            raise InstallError(f"{name!r} cannot be installed from the dashboard")
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if source.npm and shutil.which(self.npm):
                return await self._npm(name, source, on_line)
            if source.github:
                return await self._release(name, source, on_line)
            raise InstallError(f"{name} comes from npm, which this host does not have")

    async def _npm(
        self,
        name: str,
        source: Source,
        on_line: Callable[[str], Awaitable[None]] | None,
    ) -> InstalledAgent:
        assert source.npm
        folder = installs_dir(self.data) / name
        folder.mkdir(parents=True, exist_ok=True)
        result = await run(
            [
                self.npm,
                "install",
                "--prefix",
                str(folder),
                "--no-audit",
                "--no-fund",
                "--omit=dev",
                f"{source.npm}@latest",
            ],
            cwd=folder,
            timeout=NPM_TIMEOUT,
            on_line=on_line,
        )
        if not result.ok:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise InstallError(
                f"npm install {source.npm} failed: " + " | ".join(tail)
                if tail
                else f"npm install {source.npm} failed"
            )
        target = folder / "node_modules" / ".bin" / source.binary
        if not target.exists():
            raise InstallError(f"{source.npm} installed no {source.binary} command")
        manifest = folder / "node_modules" / Path(source.npm) / "package.json"
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version", "")
        self._link(source.binary, target)
        return InstalledAgent(
            name=name,
            version=str(version),
            path=str(target),
            source=source.describe(),
        )

    async def _release(
        self,
        name: str,
        source: Source,
        on_line: Callable[[str], Awaitable[None]] | None,
    ) -> InstalledAgent:
        assert source.github
        arch = _ARCH.get(platform.machine().lower())
        if arch is None or platform.system() != "Linux":
            raise InstallError(
                f"no {name} build for {platform.system()} {platform.machine()}"
            )
        asset_name = source.asset.format(arch=arch)
        async with httpx.AsyncClient(
            timeout=600,
            follow_redirects=True,
            trust_env=True,
            transport=self.transport,
            headers={"User-Agent": "auto-maintainer"},
        ) as client:
            r = await client.get(
                f"https://api.github.com/repos/{source.github}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                raise InstallError(
                    f"GitHub answered {r.status_code} for {source.github}"
                )
            release = r.json()
            version = str(release.get("tag_name", "")).removeprefix("v")
            asset = next(
                (a for a in release.get("assets", []) if a.get("name") == asset_name),
                None,
            )
            digest = str((asset or {}).get("digest") or "")
            if asset is None or not digest.startswith("sha256:"):
                raise InstallError(
                    f"{source.github} {version} has no {asset_name} with a digest"
                )
            if on_line is not None:
                await on_line(f"downloading {asset_name} ({version})")
            got = await client.get(str(asset["browser_download_url"]))
            if got.status_code != 200:
                raise InstallError(
                    f"GitHub answered {got.status_code} for {asset_name}"
                )
            archive = got.content
        if hashlib.sha256(archive).hexdigest() != digest.removeprefix("sha256:"):
            raise InstallError(
                f"{asset_name} does not match the digest GitHub lists; nothing was installed"
            )
        binary = _member(asset_name, archive, source.member or source.binary)
        folder = installs_dir(self.data) / name / version
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / source.binary
        tmp = folder / f"{source.binary}.download"
        tmp.write_bytes(binary)
        tmp.chmod(0o755)
        os.replace(tmp, target)
        self._link(source.binary, target)
        return InstalledAgent(
            name=name, version=version, path=str(target), source=source.describe()
        )

    def _link(self, binary: str, target: Path) -> None:
        """Point ``<data>/bin/<binary>`` at ``target``."""
        folder = self.data / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        link = folder / binary
        tmp = folder / f".{binary}.link"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)


def _member(name: str, archive: bytes, binary: str) -> bytes:
    """The file called ``binary`` in the release archive, read in memory: nothing
    else in it is written to disk, whatever paths it names."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile() and Path(member.name).name == binary:
                f = tar.extractfile(member)
                if f is not None:
                    return f.read()
    raise InstallError(f"{name} has no {binary}")
