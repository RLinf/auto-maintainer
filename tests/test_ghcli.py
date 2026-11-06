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

"""Installing the GitHub CLI into the data folder from its own release."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from auto_maintainer import ghcli

VERSION = "2.99.0"
ASSET = f"gh_{VERSION}_linux_amd64.tar.gz"
BINARY = b"#!/bin/sh\necho gh version 2.99.0\n"


def _archive() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in (
            (f"gh_{VERSION}_linux_amd64/bin/gh", BINARY),
            ("../../outside", b"never written"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _release(archive: bytes, digest: str | None = None) -> httpx.MockTransport:
    sums = f"{digest or hashlib.sha256(archive).hexdigest()}  {ASSET}\n"
    files = {
        f"/dl/{ASSET}": archive,
        f"/dl/gh_{VERSION}_checksums.txt": sums.encode(),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/cli/cli/releases/latest":
            return httpx.Response(
                200,
                json={
                    "tag_name": f"v{VERSION}",
                    "assets": [
                        {
                            "name": n.removeprefix("/dl/"),
                            "browser_download_url": f"https://objects.example{n}",
                        }
                        for n in files
                    ],
                },
            )
        if request.url.path in files:
            return httpx.Response(200, content=files[request.url.path])
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
def linux_without_gh(monkeypatch):
    monkeypatch.setattr(ghcli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ghcli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ghcli.shutil, "which", lambda name: None)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


async def test_the_cli_is_installed_from_its_release_into_the_data_folder(
    tmp_path: Path, linux_without_gh
) -> None:
    path = await ghcli.install(tmp_path, transport=_release(_archive()))
    gh = tmp_path / "bin" / "gh"
    assert path == str(gh) and gh.read_bytes() == BINARY
    assert os.access(gh, os.X_OK)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["gh"]
    assert not (tmp_path.parent / "outside").exists()


async def test_a_download_that_does_not_match_its_checksum_installs_nothing(
    tmp_path: Path, linux_without_gh
) -> None:
    with pytest.raises(ghcli.InstallError, match="checksum"):
        await ghcli.install(tmp_path, transport=_release(_archive(), digest="0" * 64))
    assert not (tmp_path / "bin" / "gh").exists()


async def test_a_cli_already_on_the_path_is_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ghcli.shutil, "which", lambda name: "/usr/bin/gh")
    assert await ghcli.install(tmp_path, transport=_release(b"")) == "/usr/bin/gh"
    assert not (tmp_path / "bin").exists()


def test_each_platform_gets_its_own_build() -> None:
    assert (
        ghcli.asset_name("2.1.0", "Linux", "aarch64") == "gh_2.1.0_linux_arm64.tar.gz"
    )
    assert ghcli.asset_name("2.1.0", "Darwin", "arm64") == "gh_2.1.0_macOS_arm64.zip"
    with pytest.raises(ghcli.InstallError, match="cli.github.com"):
        ghcli.asset_name("2.1.0", "Windows", "AMD64")
