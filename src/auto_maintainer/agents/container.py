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

"""Each agent run in a fresh container (design 5.7).

The container holds only what the run needs: its checkout (and that checkout's git
metadata), its run folder, the skills (read-only), the agent CLI and a few host tools
(read-only), a home folder of its own with the agent's login, and the socket of the
GitHub read proxy (``agents.github_proxy``). It holds none of the service's own
secrets, data, or other repositories, so inside it the agent may do anything: it runs
in bypass mode, with no rules on its commands. CPU, memory, and processes are capped,
every Linux capability is dropped, and no GPU is passed in. The network is open.
Environment values reach ``docker`` through its own environment (``-e NAME``), never
its command line.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..subproc import run

log = logging.getLogger(__name__)

IMAGE = "auto-maintainer-agent"
SOCKET = "/run/am/github.sock"
"""Where the GitHub read proxy's socket is inside a container."""
TOOLS_DIR = "/opt/am/bin"
"""Where the agent CLI and the host's tools are mounted inside a container."""
PATH = f"{TOOLS_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GH_PLACEHOLDER = "am-read-proxy"
"""What ``gh`` sees as its token; the proxy replaces it with the service's own."""
HOST_TOOLS = ("gh", "uv", "uvx", "ruff")
"""Host tools mounted read-only when the host has them."""


def dockerfile() -> str:
    """The image every agent container starts from."""
    return (
        resources.files("auto_maintainer.agents")
        .joinpath("agent.Dockerfile")
        .read_text(encoding="utf-8")
    )


def image_tag(base: str = IMAGE) -> str:
    """The image name with a tag from its Dockerfile, so a change builds a new one."""
    digest = hashlib.sha256(dockerfile().encode()).hexdigest()[:12]
    return f"{base}:{digest}"


def host_tools(names: Sequence[str] = HOST_TOOLS) -> dict[str, Path]:
    """The host's own copies of ``names`` that exist, by name."""
    found: dict[str, Path] = {}
    for name in names:
        path = shutil.which(name)
        if path:
            found[name] = Path(os.path.realpath(path))
    return found


def git_dirs(workdir: Path) -> tuple[Path | None, Path | None]:
    """A git worktree's own metadata folder and the repository it belongs to, so git
    works inside the container; ``(None, None)`` for a plain folder."""
    dot = workdir / ".git"
    if not dot.is_file():
        return None, None
    text = dot.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None, None
    gitdir = Path(text.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (workdir / gitdir).resolve()
    common = gitdir / "commondir"
    commondir = (
        (gitdir / common.read_text().strip()).resolve() if common.is_file() else None
    )
    return gitdir, commondir


@dataclass(frozen=True)
class Prepared:
    argv: list[str]
    """The ``docker run`` command line."""
    env: dict[str, str]
    """The environment of the ``docker`` process: it passes the values on."""
    name: str
    """The container's name, to stop it."""


class ContainerSandbox:
    """Runs agent CLIs in containers of ``image``.

    Args:
        image: The image, built from ``agent.Dockerfile``.
        docker: The docker binary.
        cpus: CPUs a container may use.
        memory: Memory a container may use, such as ``8g``.
        pids: Processes a container may run.
        network: The docker network; ``bridge`` gives open outbound access.
        github_socket: The GitHub read proxy's socket on the host, if it runs.
        tools: Host tools to mount, by name (``host_tools()``).
        mounts: More host paths to mount read-only at the same place.
        home: The home folder path agents see; the host's own by default.
    """

    def __init__(
        self,
        image: str,
        *,
        docker: str = "docker",
        cpus: float = 2.0,
        memory: str = "8g",
        pids: int = 1024,
        network: str = "bridge",
        github_socket: Path | None = None,
        tools: Mapping[str, Path] | None = None,
        mounts: Sequence[Path] = (),
        home: Path | None = None,
    ) -> None:
        self.image = image
        self.docker = docker
        self.cpus = cpus
        self.memory = memory
        self.pids = pids
        self.network = network
        self.github_socket = github_socket
        self.tools = dict(tools or {})
        self.mounts = list(mounts)
        self.home = home or Path.home()

    async def ready(self) -> bool:
        """Whether docker answers and the image is there."""
        result = await run([self.docker, "image", "inspect", self.image], timeout=30)
        return result.ok

    async def build(self) -> str:
        """Build the image from ``agent.Dockerfile``; the build's log."""
        network = "host" if self.network == "host" else "default"
        result = await run(
            [self.docker, "build", "--network", network, "-t", self.image, "-"],
            stdin=dockerfile(),
            timeout=3600,
        )
        if not result.ok:
            raise RuntimeError(
                f"the agent image did not build: {result.stderr[-2000:]}"
            )
        return result.stdout[-2000:]

    async def stop(self, name: str) -> None:
        """Remove the container ``name`` if it still runs."""
        await run([self.docker, "rm", "-f", name], timeout=60)

    def prepare(
        self,
        run_id: str,
        workdir: Path,
        run_dir: Path,
        binary: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        rw: Sequence[Path] = (),
        ro: Sequence[Path] = (),
        home_paths: Sequence[str] = (),
    ) -> Prepared:
        """The ``docker run`` that runs ``argv`` (whose first word is ``binary``) for
        one agent run, with ``env`` inside the container."""
        name = f"am-{run_id}"
        home_dir = run_dir / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        binds: list[tuple[Path, str, bool]] = [(workdir, str(workdir), False)]
        gitdir, commondir = git_dirs(workdir)
        if commondir is not None:
            binds.append((commondir, str(commondir), True))
        if gitdir is not None:
            binds.append((gitdir, str(gitdir), False))
        binds.append((run_dir, str(run_dir), False))
        binds.append((home_dir, str(self.home), False))
        for rel in home_paths:
            source = self.home / rel
            if source.exists():
                binds.append((source, str(self.home / rel), False))
        binds += [(p, str(p), False) for p in rw if p.exists()]
        binds += [(p, str(p), True) for p in [*ro, *self.mounts] if p.exists()]
        real = Path(os.path.realpath(binary))
        inside = f"{TOOLS_DIR}/{Path(binary).name}"
        binds.append((real, inside, True))
        for tool, path in self.tools.items():
            binds.append((path, f"{TOOLS_DIR}/{tool}", True))
        if self.github_socket is not None and self.github_socket.exists():
            binds.append((self.github_socket, SOCKET, False))

        # Not secret, and ``docker`` needs its own PATH and HOME: on the command line.
        plain = {"HOME": str(self.home), "PATH": PATH}
        inner = {k: v for k, v in env.items() if k not in plain}
        if self.github_socket is not None:
            inner["GH_TOKEN"] = GH_PLACEHOLDER
        cmd = [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--init",
            "--name",
            name,
            "--network",
            self.network,
            "--cpus",
            str(self.cpus),
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-w",
            str(workdir),
        ]
        seen: set[str] = set()
        for source, target, readonly in binds:
            if target in seen:
                continue
            seen.add(target)
            spec = f"type=bind,src={source},dst={target}"
            cmd += ["--mount", spec + (",readonly" if readonly else "")]
        for key, value in plain.items():
            cmd += ["-e", f"{key}={value}"]
        for key in sorted(inner):
            cmd += ["-e", key]
        cmd += [self.image, inside, *argv[1:]]
        outer = {
            **inner,
            **{k: v for k, v in os.environ.items() if k.startswith("DOCKER_")},
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
        }
        return Prepared(argv=cmd, env=outer, name=name)


class SandboxStarter:
    """Readies the sandbox while the service starts: when docker answers, it builds
    the image if it is missing (in the background, which takes minutes once) and then
    gives the sandbox to every agent runtime, including those the service settings
    build later (``attach``). Until then, and on a host without docker, agents run on
    the host under the automatic rules.

    Args:
        sandbox: The sandbox to ready.
        runtimes: The agent runtimes as they are now.
    """

    name = "agent sandbox"

    def __init__(
        self, sandbox: ContainerSandbox, runtimes: Callable[[], Mapping[str, object]]
    ) -> None:
        self.sandbox = sandbox
        self._runtimes = runtimes
        self.ready = False
        self.status = "not started"
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._prepare(), name="agent-sandbox")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _prepare(self) -> None:
        if shutil.which(self.sandbox.docker) is None:
            self.status = "docker is not installed: agents run on the host"
            log.warning(self.status)
            return
        if not await self.sandbox.ready():
            self.status = f"building the agent image {self.sandbox.image}"
            log.info(self.status)
            try:
                await self.sandbox.build()
            except Exception as exc:
                self.status = f"the agent image did not build: {exc}"
                log.error(self.status)
                return
        self.ready = True
        self.attach(self._runtimes())
        self.status = f"agents run in containers of {self.sandbox.image}"
        log.info(self.status)

    def attach(self, runtimes: Mapping[str, object]) -> None:
        """Give the sandbox to the runtimes that can run in it, once it is ready."""
        if not self.ready:
            return
        for runtime in runtimes.values():
            if hasattr(runtime, "home_paths"):
                runtime.container_sandbox = self.sandbox  # type: ignore[attr-defined]
