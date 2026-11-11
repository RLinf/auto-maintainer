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
GitHub read proxy (``forges.github.proxy``). It holds none of the service's own
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
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ..domain.ports import CommandResult
from ..subproc import run

log = logging.getLogger(__name__)

IMAGE = "auto-maintainer-agent"
SOCKET = "/run/am/github.sock"
LAB_SOCKET = "/run/am/lab.sock"
RECORDS_SOCKET = "/run/am/records.sock"
"""Where a reproduction's agent reaches its session on a test machine."""
"""Where the GitHub read proxy's socket is inside a container."""
TOOLS_DIR = "/opt/am/bin"
"""Where the agent CLI and the host's tools are mounted inside a container."""
PATH = f"{TOOLS_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GH_PLACEHOLDER = "am-read-proxy"
"""What ``gh`` sees as its token; the proxy replaces it with the service's own."""
HOST_TOOLS = ("gh", "uv", "uvx", "ruff", "node")
"""Host tools mounted read-only when the host has them. ``node`` runs the agent CLIs
installed from npm, whose Node is newer than the image's."""


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


def git_dirs(
    workdir: Path, roots: Sequence[Path] = ()
) -> tuple[Path | None, Path | None]:
    """A git worktree's own metadata folder and the repository it belongs to, so git
    works inside the container; ``(None, None)`` for a plain folder.

    The checkout's ``.git`` file and its metadata folder are an earlier agent's to
    write, so neither is believed on its own: the metadata folder must be
    ``<repository>/worktrees/<name>`` of a repository under ``roots`` (the service's
    mirrors) and point back at this checkout. Otherwise git gets nothing mounted,
    instead of whatever folder the file names."""
    dot = workdir / ".git"
    if dot.is_symlink() or not dot.is_file():
        return None, None
    text = dot.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None, None
    named = Path(text.split(":", 1)[1].strip())
    gitdir = Path(os.path.realpath(named if named.is_absolute() else workdir / named))
    common = gitdir.parent.parent
    if gitdir.parent.name != "worktrees" or not common.is_dir():
        return None, None
    if roots and not any(
        common.is_relative_to(os.path.realpath(root)) for root in roots
    ):
        return None, None
    try:
        back = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if os.path.realpath(back) != os.path.realpath(dot):
        return None, None
    return gitdir, common


def make_inside(root: Path, rel: Path, *, folder: bool) -> None:
    """Create ``root / rel`` (a folder, or an empty file when not ``folder``) and the
    folders above it as the service's user. ``docker`` creates a mount point that is
    missing as root, and the folders above it too, so a CLI running as the service's
    user could not then make its own folders beside them (``~/.local/state`` beside a
    mounted ``~/.local/share/opencode``). A link on the way stops it: an agent of an
    earlier attempt could have left one, and nothing here follows it out of ``root``."""
    current = root
    for i, part in enumerate(rel.parts):
        current = current / part
        if current.is_symlink():
            return
        if current.exists():
            continue
        if folder or i < len(rel.parts) - 1:
            current.mkdir()
        else:
            current.touch()


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
        installs: Where the dashboard installs agent CLIs (``agents/install.py``).
        home: The home folder path agents see; the host's own by default.
        git_roots: Where the repositories of the checkouts agents work in are (the
            workspace's mirrors): a checkout's git metadata is mounted only from there.
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
        installs: Path | None = None,
        home: Path | None = None,
        git_roots: Sequence[Path] = (),
    ) -> None:
        self.image = image
        self.git_roots = list(git_roots)
        self.docker = docker
        self.cpus = cpus
        self.memory = memory
        self.pids = pids
        self.network = network
        self.github_socket = github_socket
        self.tools = dict(tools or {})
        self.mounts = list(mounts)
        self.installs = installs
        """Where the dashboard installs agent CLIs (``agents/install.py``); a CLI
        from there runs in place, with its folder mounted read-only."""
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

    async def remove_leftovers(self) -> int:
        """Remove agent containers a previous process left running (it was killed
        before it could): their agents would go on beside the ones that resume their
        work. Returns how many."""
        listed = await run(
            [self.docker, "ps", "-aq", "--filter", "name=^am-run_"], timeout=60
        )
        ids = listed.stdout.split() if listed.ok else []
        if ids:
            await run([self.docker, "rm", "-f", *ids], timeout=120)
        return len(ids)

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
        home_source: Path | None = None,
    ) -> Prepared:
        """The ``docker run`` that runs ``argv`` (whose first word is ``binary``) for
        one agent run, with ``env`` inside the container. ``home_paths`` come from
        ``home_source`` (a person's own home folder for the CLI) instead of the
        host's home when it is set; inside, they are where the CLI looks."""
        name = f"am-{run_id}"
        home_dir = run_dir / "home"
        if home_dir.is_symlink():
            home_dir.unlink()
        home_dir.mkdir(parents=True, exist_ok=True)
        binds: list[tuple[Path, str, bool]] = [(workdir, str(workdir), False)]
        gitdir, commondir = git_dirs(workdir, self.git_roots)
        if commondir is not None:
            binds.append((commondir, str(commondir), True))
        if gitdir is not None:
            binds.append((gitdir, str(gitdir), False))
        binds.append((run_dir, str(run_dir), False))
        binds.append((home_dir, str(self.home), False))
        for rel in home_paths:
            source = (home_source or self.home) / rel
            if source.exists():
                binds.append((source, str(self.home / rel), False))
        binds += [(p, str(p), False) for p in rw if p.exists()]
        binds += [(p, str(p), True) for p in [*ro, *self.mounts] if p.exists()]
        real = Path(os.path.realpath(binary))
        installs = Path(os.path.realpath(self.installs)) if self.installs else None
        if installs is not None and real.is_relative_to(installs):
            # An npm package needs its whole folder; it runs where it lies.
            package = installs / real.relative_to(installs).parts[0]
            binds.append((package, str(package), True))
            inside = str(real)
        else:
            inside = f"{TOOLS_DIR}/{Path(binary).name}"
            binds.append((real, inside, True))
        for tool, path in self.tools.items():
            binds.append((path, f"{TOOLS_DIR}/{tool}", True))
        if self.github_socket is not None and self.github_socket.exists():
            binds.append((self.github_socket, SOCKET, False))
        lab_socket = env.get("AM_LAB_SOCKET") if env.get("AM_LAB_SESSION") else None
        if lab_socket and Path(lab_socket).exists():
            binds.append((Path(lab_socket), LAB_SOCKET, False))
        records = env.get("AM_RECORDS_SOCKET") if env.get("AM_RECORDS_TOKEN") else None
        if records and Path(records).exists():
            binds.append((Path(records), RECORDS_SOCKET, False))

        # Not secret, and ``docker`` needs its own PATH and HOME: on the command line.
        bin_dir = run_dir / "bin"
        plain = {
            "HOME": str(self.home),
            "PATH": f"{bin_dir}:{PATH}" if bin_dir.is_dir() else PATH,
        }
        inner = {k: v for k, v in env.items() if k not in plain}
        if lab_socket:
            inner["AM_LAB_SOCKET"] = LAB_SOCKET
        else:
            inner.pop("AM_LAB_SOCKET", None)
        if records:
            inner["AM_RECORDS_SOCKET"] = RECORDS_SOCKET
        else:
            inner.pop("AM_RECORDS_SOCKET", None)
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
            inside_home = Path(target)
            if inside_home != self.home and inside_home.is_relative_to(self.home):
                make_inside(
                    home_dir,
                    inside_home.relative_to(self.home),
                    folder=source.is_dir(),
                )
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
    NETWORKS = ("bridge", "host")

    def __init__(
        self,
        sandbox: ContainerSandbox,
        runtimes: Callable[[], Mapping[str, object]],
        *,
        enabled: bool = True,
        save: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self._runtimes = runtimes
        self.enabled = enabled
        self._save = save
        self.ready = False
        self.status = "not started"
        self._task: asyncio.Task[None] | None = None

    def describe(self) -> dict[str, object]:
        """What the Settings page shows."""
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "status": self.status,
            "docker": shutil.which(self.sandbox.docker) is not None,
            "image": self.sandbox.image,
            "network": self.sandbox.network,
            "cpus": self.sandbox.cpus,
            "memory": self.sandbox.memory,
            "pids": self.sandbox.pids,
        }

    async def configure(
        self, *, enabled: bool, network: str, cpus: float, memory: str
    ) -> None:
        """Change the sandbox while the service runs: the network and the limits
        apply to the next agent run; turning it off gives agents back to the host at
        once, and turning it on readies it (building the image if needed). The
        settings are saved to the host's ``local.yaml``."""
        if network not in self.NETWORKS:
            raise ValueError(f"network must be one of {', '.join(self.NETWORKS)}")
        self.sandbox.network = network
        self.sandbox.cpus = cpus
        self.sandbox.memory = memory
        if self._save is not None:
            self._save(
                {"enabled": enabled, "network": network, "cpus": cpus, "memory": memory}
            )
        if not enabled and self.enabled:
            self.enabled = False
            await self.stop()
            self.ready = False
            for runtime in self._runtimes().values():
                if hasattr(runtime, "home_paths"):
                    runtime.container_sandbox = None  # type: ignore[attr-defined]
            self.status = "off: agents run on the host"
        elif enabled and not self.enabled:
            self.enabled = True
            await self.start()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._prepare(), name="agent-sandbox")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _prepare(self) -> None:
        if not self.enabled:
            self.status = "off: agents run on the host"
            return
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
        with contextlib.suppress(Exception):
            left = await self.sandbox.remove_leftovers()
            if left:
                log.info("removed %d agent containers a previous process left", left)
        self.ready = True
        self.attach(self._runtimes())
        self.status = f"agents run in containers of {self.sandbox.image}"
        log.info(self.status)

    def attach(self, runtimes: Mapping[str, object]) -> None:
        """Give the sandbox to the runtimes that can run in it, once it is ready."""
        if not (self.ready and self.enabled):
            return
        for runtime in runtimes.values():
            if hasattr(runtime, "home_paths"):
                runtime.container_sandbox = self.sandbox  # type: ignore[attr-defined]


class ContainedSandbox:
    """The ``Sandbox`` port: a command whose code an agent wrote (``untrusted``), such
    as the tests of a change the service made to itself, runs in a fresh container of
    the agents' image, holding its checkout and nothing of the service, as the agent
    did; every other command, and every command while the host has no container for
    agents (where agents run on the host too), goes to ``fallback``.

    Args:
        starter: The agents' sandbox, once ready.
        fallback: Where the rest runs (``workspace.git.LocalSandbox``).
        root: A folder for each command's home, and the package caches commands
            share (``uv``), so a second run of the tests does not download again.
    """

    def __init__(self, starter: SandboxStarter, fallback: Any, root: Path) -> None:
        self.starter = starter
        self.fallback = fallback
        self.root = root

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        untrusted: bool = False,
    ) -> CommandResult:
        if not (untrusted and self.starter.ready and self.starter.enabled):
            return await self.fallback.run(
                argv, cwd=cwd, env=env, timeout=timeout, untrusted=untrusted
            )
        binary = shutil.which(argv[0]) or argv[0]
        name = f"check_{uuid.uuid4().hex[:12]}"
        run_dir = self.root / "runs" / name
        run_dir.mkdir(parents=True)
        cache = self.root / "uv-cache"
        cache.mkdir(parents=True, exist_ok=True)
        inner = {"LANG": "C.UTF-8", "UV_CACHE_DIR": str(cache), **(env or {})}
        box = self.starter.sandbox
        prepared = box.prepare(
            name, Path(cwd), run_dir, binary, argv, inner, rw=[cache]
        )
        try:
            result = await run(prepared.argv, env=prepared.env, timeout=timeout)
        finally:
            with contextlib.suppress(Exception):
                await box.stop(prepared.name)
            shutil.rmtree(run_dir, ignore_errors=True)
        return CommandResult(
            exit_code=result.exit_code,
            stdout=result.stdout[-20_000:],
            stderr=result.stderr[-20_000:],
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
        )
