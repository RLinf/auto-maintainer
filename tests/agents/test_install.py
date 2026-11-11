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

"""Installing agent CLIs (``agents/install.py``), running them in a container where
they were installed, and the DeepSeek Harness adapter."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import tarfile
from pathlib import Path

import httpx
import pytest

from auto_maintainer.agents.adapters.dsh import DshRuntime
from auto_maintainer.agents.container import ContainerSandbox
from auto_maintainer.agents.exec import RunFiles
from auto_maintainer.agents.install import AgentInstaller, InstallError
from auto_maintainer.domain.agents import AgentTask, TaskKind
from auto_maintainer.domain.settings import ProviderBinding

FAKE_NPM = """#!/bin/sh
# A stand-in for npm: the registry's latest version is in ./version beside it (9.9.9
# without one); install puts the named package's command into --prefix.
ver=$(cat "$(dirname "$0")/version" 2>/dev/null || echo 9.9.9)
if [ "$1" = view ]; then echo "$ver"; exit 0; fi
prefix="$3"
mkdir -p "$prefix/node_modules/@openai/codex/bin" "$prefix/node_modules/.bin"
printf '{"name": "@openai/codex", "version": "%s"}' "$ver" > "$prefix/node_modules/@openai/codex/package.json"
printf '#!/bin/sh\\necho codex' > "$prefix/node_modules/@openai/codex/bin/codex.js"
chmod +x "$prefix/node_modules/@openai/codex/bin/codex.js"
ln -sf ../@openai/codex/bin/codex.js "$prefix/node_modules/.bin/codex"
echo "added 1 package"
"""


async def test_an_npm_package_is_installed_and_linked_from_the_data_bin(
    tmp_path: Path,
) -> None:
    npm = tmp_path / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    data = tmp_path / "data"
    installer = AgentInstaller(data, npm=str(npm))
    assert installer.sources()["codex"] == "npm @openai/codex"
    # Every CLI the service runs comes from its publisher's own npm package or release.
    assert installer.sources()["opencode"] == "npm opencode-ai"
    assert installer.sources()["gemini"] == "npm @google/gemini-cli"
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    assert await installer.latest("codex") == "9.9.9"
    done = await installer.install("codex", on_line)
    assert (done.version, done.source) == ("9.9.9", "npm @openai/codex")
    link = data / "bin" / "codex"
    assert os.path.realpath(link) == os.path.realpath(
        data / "agents/codex/9.9.9/node_modules/@openai/codex/bin/codex.js"
    )
    assert "added 1 package" in lines
    with pytest.raises(InstallError, match="cannot be installed"):
        await installer.install("not-an-agent")


async def test_an_upgrade_installs_beside_the_running_version_and_keeps_one_before(
    tmp_path: Path,
) -> None:
    npm = tmp_path / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    data = tmp_path / "data"
    agent = data / "agents" / "codex"
    # An install from before versions had folders of their own.
    (agent / "node_modules" / ".bin").mkdir(parents=True)
    (agent / "package.json").write_text("{}")
    installer = AgentInstaller(data, npm=str(npm))

    def release(version: str) -> None:
        (tmp_path / "version").write_text(version)

    release("1.0.0")
    await installer.install("codex")
    assert (agent / "node_modules").is_dir(), "the version before stays for its runs"
    release("1.1.0")
    assert await installer.latest("codex") == "1.1.0"
    running = os.path.realpath(data / "bin" / "codex")
    await installer.install("codex")
    assert os.path.exists(running), "a run that started on 1.0.0 keeps its files"
    assert os.path.realpath(data / "bin" / "codex").startswith(str(agent / "1.1.0"))
    assert (
        not (agent / "node_modules").exists() and not (agent / "package.json").exists()
    )
    release("1.2.0")
    await installer.install("codex")
    assert sorted(p.name for p in agent.iterdir()) == ["1.1.0", "1.2.0"]
    again = await installer.install("codex")
    assert again.version == "1.2.0", "the same version again changes nothing"


async def test_a_failed_npm_install_leaves_the_current_version_in_place(
    tmp_path: Path,
) -> None:
    npm = tmp_path / "npm"
    npm.write_text(
        FAKE_NPM.replace('prefix="$3"', 'prefix="$3"; [ "$ver" = 2.0.0 ] && exit 1')
    )
    npm.chmod(0o755)
    data = tmp_path / "data"
    installer = AgentInstaller(data, npm=str(npm))
    (tmp_path / "version").write_text("1.0.0")
    await installer.install("codex")
    (tmp_path / "version").write_text("2.0.0")
    with pytest.raises(InstallError, match="npm install @openai/codex failed"):
        await installer.install("codex")
    assert os.path.realpath(data / "bin" / "codex").startswith(
        str(data / "agents" / "codex" / "1.0.0")
    )
    assert sorted(p.name for p in (data / "agents" / "codex").iterdir()) == ["1.0.0"]


def _release(archive: bytes, digest: str) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": "1.5.3",
                    "assets": [
                        {
                            "name": "agy_cli_linux_x64.tar.gz",
                            "digest": f"sha256:{digest}",
                            "browser_download_url": "https://github.com/dl/agy.tgz",
                        }
                    ],
                },
            )
        return httpx.Response(200, content=archive)

    return httpx.MockTransport(answer)


def _archive() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        body = b"#!/bin/sh\necho 1.5.3\n"
        info = tarfile.TarInfo("antigravity")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


@pytest.mark.skipif(
    platform.system() != "Linux" or platform.machine() != "x86_64",
    reason="the release asset is chosen for linux x64",
)
async def test_a_release_build_must_match_its_published_digest(tmp_path: Path) -> None:
    archive = _archive()
    tampered = AgentInstaller(
        tmp_path / "bad", npm="no-npm-here", transport=_release(archive, "0" * 64)
    )
    with pytest.raises(InstallError, match="does not match the digest"):
        await tampered.install("antigravity")
    assert not (tmp_path / "bad" / "bin" / "agy").exists()

    data = tmp_path / "data"
    good = AgentInstaller(
        data,
        npm="no-npm-here",
        transport=_release(archive, hashlib.sha256(archive).hexdigest()),
    )
    assert "codex" not in good.sources(), "npm packages need npm"
    done = await good.install("antigravity")
    assert done.version == "1.5.3"
    assert (data / "bin" / "agy").read_bytes() == b"#!/bin/sh\necho 1.5.3\n"


def test_an_installed_cli_runs_in_place_from_its_read_only_folder(
    tmp_path: Path,
) -> None:
    installs = tmp_path / "agents"
    script = installs / "codex/node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("x")
    link = tmp_path / "bin" / "codex"
    link.parent.mkdir()
    link.symlink_to(script)
    work, run = tmp_path / "work", tmp_path / "run"
    work.mkdir()
    run.mkdir()
    sandbox = ContainerSandbox("img", installs=installs, home=tmp_path / "home")
    argv = sandbox.prepare("r1", work, run, str(link), [str(link), "exec"], {}).argv
    package = os.path.realpath(installs / "codex")
    assert f"type=bind,src={package},dst={package},readonly" in argv
    assert argv[argv.index("img") + 1] == os.path.realpath(script)


def test_dsh_runs_its_acp_profile_on_the_agents_provider(tmp_path: Path) -> None:
    runtime = DshRuntime(
        provider=ProviderBinding(
            id="ds",
            name="DeepSeek gateway",
            protocol="openai",
            base_url="https://gateway.example/v1",
            api_key="k-123",
            models=("deepseek-v4-flash",),
        )
    )
    task = AgentTask(kind=TaskKind.REVIEW, prompt="p", workdir=tmp_path, effort="xhigh")
    files = RunFiles.create(tmp_path, "r1")
    assert runtime.acp_argv(task, files, "/x/dsh") == ["/x/dsh", "--profile", "acp"]
    assert runtime.provider_env() == {
        "DEEPSEEK_API_KEY": "k-123",
        "DEEPSEEK_BASE_URL": "https://gateway.example/v1",
    }
    assert runtime.task_env(task)["DSH_HOME"].endswith("/.dsh")
    assert (
        "session/set_config_option",
        {"configId": "reasoning_effort", "value": "max"},
    ) in runtime.session_setup(task, {})
