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

"""The GitHub CLI, through which the service acts and people sign in, and installing it.

``install`` downloads the latest release of the GitHub CLI (``cli/cli`` on GitHub)
for this platform into ``<data>/bin``, checks the archive against the release's
checksums file, and puts that folder first on ``PATH``, so every lookup of ``gh`` in
this process and in the processes it starts finds it. Nothing outside the data
folder changes, and no administrator rights are needed. ``add_to_path`` does the
last step at startup, for a CLI installed by an earlier run.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import platform
import shutil
import tarfile
import zipfile
from pathlib import Path

import httpx

RELEASES = "https://api.github.com/repos/cli/cli/releases/latest"
"""The GitHub CLI's latest release; its assets are the builds and their checksums."""

_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "armv6",
    "armv6l": "armv6",
    "i386": "386",
    "i686": "386",
}

_lock = asyncio.Lock()


class InstallError(RuntimeError):
    """The GitHub CLI could not be installed; the message says why."""


def bin_dir(data: Path) -> Path:
    return data / "bin"


def add_to_path(data: Path) -> None:
    """Put ``<data>/bin`` first on this process's ``PATH`` (once)."""
    folder = str(bin_dir(data))
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if folder not in parts:
        os.environ["PATH"] = os.pathsep.join([folder, *parts])


def asset_name(
    version: str, system: str | None = None, machine: str | None = None
) -> str:
    """The release asset of ``version`` for this platform (or the one named)."""
    system = system or platform.system()
    arch = _ARCH.get((machine or platform.machine()).lower())
    if arch is None:
        raise InstallError(
            f"the GitHub CLI has no build for {machine or platform.machine()}"
        )
    if system == "Linux":
        return f"gh_{version}_linux_{arch}.tar.gz"
    if system == "Darwin":
        return f"gh_{version}_macOS_{arch}.zip"
    raise InstallError(
        f"the GitHub CLI cannot be installed from here on {system}; "
        "install it from https://cli.github.com"
    )


async def install(
    data: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Install the GitHub CLI into ``<data>/bin`` and return its path; when ``gh`` is
    already on ``PATH``, return that one and download nothing.

    Raises:
        InstallError: No build for this platform, GitHub did not answer, or the
            download does not match the release's checksum.
    """
    async with _lock:
        found = shutil.which("gh")
        if found:
            return found
        async with httpx.AsyncClient(
            timeout=180,
            follow_redirects=True,
            trust_env=True,
            transport=transport,
            headers={"User-Agent": "auto-maintainer"},
        ) as client:
            r = await client.get(
                RELEASES, headers={"Accept": "application/vnd.github+json"}
            )
            if r.status_code != 200:
                raise InstallError(
                    f"GitHub answered {r.status_code} when asked for the GitHub CLI"
                )
            release = r.json()
            version = str(release.get("tag_name", "")).removeprefix("v")
            name = asset_name(version)
            sums_name = f"gh_{version}_checksums.txt"
            urls = {
                a.get("name"): a.get("browser_download_url")
                for a in release.get("assets", [])
            }
            if not urls.get(name) or not urls.get(sums_name):
                raise InstallError(f"the GitHub CLI {version} has no {name}")
            archive = await _download(client, urls[name])
            sums = (await _download(client, urls[sums_name])).decode("utf-8", "replace")
        expected = {
            parts[1].lstrip("*"): parts[0].lower()
            for parts in (line.split() for line in sums.splitlines())
            if len(parts) == 2
        }
        if hashlib.sha256(archive).hexdigest() != expected.get(name):
            raise InstallError(
                f"{name} does not match the checksum the release lists; nothing was installed"
            )
        binary = _extract(name, archive)
        folder = bin_dir(data)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "gh.download"
        tmp.write_bytes(binary)
        tmp.chmod(0o755)
        os.replace(tmp, folder / "gh")
        add_to_path(data)
        return str(folder / "gh")


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    r = await client.get(url)
    if r.status_code != 200:
        raise InstallError(f"GitHub answered {r.status_code} for {url}")
    return r.content


def _extract(name: str, archive: bytes) -> bytes:
    """``bin/gh`` from the release archive, read in memory: nothing else in it is
    written to disk, whatever paths it names."""
    if name.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith("/bin/gh"):
                    f = tar.extractfile(member)
                    if f is not None:
                        return f.read()
    else:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for member in z.namelist():
                if member.endswith("/bin/gh"):
                    return z.read(member)
    raise InstallError(f"{name} has no bin/gh")
