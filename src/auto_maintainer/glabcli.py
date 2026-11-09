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

"""The GitLab CLI (``glab``), through which the service acts on a GitLab forge and
people sign in there, and installing it (design 5.16).

``install`` downloads the latest release of the GitLab CLI (``gitlab-org/cli`` on
gitlab.com) for this platform into ``<data>/bin``, beside the GitHub CLI
(``ghcli.py``), and checks the archive against the release's checksums file. Nothing
outside the data folder changes, and no administrator rights are needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import platform
import shutil
import tarfile
from pathlib import Path

import httpx

from .ghcli import add_to_path, bin_dir

RELEASE = (
    "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases/permalink/latest"
)
"""The GitLab CLI's latest release; its links are the builds and their checksums."""

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_lock = asyncio.Lock()


class InstallError(RuntimeError):
    """The GitLab CLI could not be installed; the message says why."""


def asset_name(version: str, machine: str | None = None) -> str:
    arch = _ARCH.get((machine or platform.machine()).lower())
    if arch is None or platform.system() != "Linux":
        raise InstallError(
            f"the GitLab CLI cannot be installed from here on {platform.system()} "
            f"{platform.machine()}; install it from https://gitlab.com/gitlab-org/cli"
        )
    return f"glab_{version}_linux_{arch}.tar.gz"


async def install(
    data: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Install the GitLab CLI into ``<data>/bin`` and return its path; when ``glab``
    is already on ``PATH``, return that one and download nothing.

    Raises:
        InstallError: No build for this platform, GitLab did not answer, or the
            download does not match the release's checksum.
    """
    async with _lock:
        found = shutil.which("glab")
        if found:
            return found
        async with httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            trust_env=True,
            transport=transport,
            headers={"User-Agent": "auto-maintainer"},
        ) as client:
            r = await client.get(RELEASE)
            if r.status_code != 200:
                raise InstallError(
                    f"GitLab answered {r.status_code} for the GitLab CLI"
                )
            release = r.json()
            version = str(release.get("tag_name", "")).removeprefix("v")
            name = asset_name(version)
            links = {
                link.get("name"): link.get("direct_asset_url") or link.get("url")
                for link in (release.get("assets") or {}).get("links") or []
            }
            if not links.get(name) or not links.get("checksums.txt"):
                raise InstallError(f"the GitLab CLI {version} has no {name}")
            archive = (await client.get(links[name])).content
            sums = (await client.get(links["checksums.txt"])).text
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
        tmp = folder / "glab.download"
        tmp.write_bytes(binary)
        tmp.chmod(0o755)
        os.replace(tmp, folder / "glab")
        add_to_path(data)
        return str(folder / "glab")


def _extract(name: str, archive: bytes) -> bytes:
    """``bin/glab`` from the release archive, read in memory."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith("bin/glab"):
                f = tar.extractfile(member)
                if f is not None:
                    return f.read()
    raise InstallError(f"{name} has no bin/glab")
