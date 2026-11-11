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

"""Agents in containers: what each run's container holds, bypass inside it, and its
removal when the run ends."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime
from auto_maintainer.agents.container import (
    GH_PLACEHOLDER,
    SOCKET,
    TOOLS_DIR,
    ContainerSandbox,
    Prepared,
    SandboxStarter,
)
from auto_maintainer.agents.registry import build_runtime
from auto_maintainer.agents.router import effective_permissions
from auto_maintainer.domain.agents import AgentEventType, AgentTask, TaskKind
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.settings import RuntimeConfig


def _mounts(argv: list[str]) -> dict[str, tuple[str, bool]]:
    out = {}
    for i, word in enumerate(argv):
        if word == "--mount":
            fields = dict(f.split("=", 1) for f in argv[i + 1].split(",") if "=" in f)
            out[fields["dst"]] = (fields["src"], argv[i + 1].endswith(",readonly"))
    return out


def test_a_container_holds_the_run_and_nothing_of_the_service(tmp_path: Path) -> None:
    repo = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    work = tmp_path / "work"
    gitdir = repo / "worktrees" / "work"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {gitdir}\n")
    (gitdir / "gitdir").write_text(f"{work / '.git'}\n")
    run_dir, skills, home = tmp_path / "run", tmp_path / "skills", tmp_path / "home"
    run_dir.mkdir()
    skills.mkdir()
    (home / ".gemini").mkdir(parents=True)
    (home / ".ssh").mkdir()
    socket = tmp_path / "github.sock"
    socket.write_text("")
    binary = tmp_path / "agy"
    binary.write_text("")
    box = ContainerSandbox(
        "agent:1",
        cpus=1.5,
        memory="4g",
        github_socket=socket,
        tools={"gh": tmp_path / "gh-bin"},
        home=home,
    )
    prepared = box.prepare(
        "run_1",
        work,
        run_dir,
        str(binary),
        [str(binary), "acp", "--x"],
        {"GEMINI_API_KEY": "secret-value", "LANG": "C.UTF-8"},
        ro=[skills],
        home_paths=[".gemini", ".missing"],
    )
    argv = prepared.argv
    assert argv[:3] == ["docker", "run", "--rm"] and prepared.name == "am-run_1"
    assert argv[-3:] == [f"{TOOLS_DIR}/agy", "acp", "--x"] and "agent:1" in argv
    mounts = _mounts(argv)
    assert mounts[str(work)] == (str(work), False)
    assert mounts[str(gitdir)] == (str(gitdir), False)
    assert mounts[str(repo.resolve())][1] is True, "the repository itself read-only"
    assert mounts[str(home)] == (str(run_dir / "home"), False), "a home of its own"
    assert mounts[str(home / ".gemini")] == (str(home / ".gemini"), False)
    assert str(home / ".ssh") not in mounts and str(home / ".missing") not in mounts
    assert mounts[str(skills)][1] is True
    assert mounts[f"{TOOLS_DIR}/gh"][1] is True and mounts[SOCKET][0] == str(socket)
    for flag, value in (("--cpus", "1.5"), ("--memory", "4g"), ("--cap-drop", "ALL")):
        assert argv[argv.index(flag) + 1] == value
    assert "secret-value" not in " ".join(argv), "secrets never on the command line"
    assert argv[argv.index("GEMINI_API_KEY") - 1] == "-e"
    assert prepared.env["GEMINI_API_KEY"] == "secret-value"
    assert prepared.env["GH_TOKEN"] == GH_PLACEHOLDER
    assert f"HOME={home}" in argv and any(
        a.startswith("PATH=/opt/am/bin") for a in argv
    )


def test_git_metadata_is_mounted_only_for_the_checkouts_own_worktree(
    tmp_path: Path,
) -> None:
    """An earlier agent can rewrite the checkout's ``.git`` file; the folder it names
    is mounted only when it is this checkout's worktree of a mirror the service
    keeps, never another folder of the host."""
    from auto_maintainer.agents.container import git_dirs

    mirrors = tmp_path / "mirrors"
    repo = mirrors / "acme__widget.git"
    admin = repo / "worktrees" / "work"
    admin.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (admin / "gitdir").write_text(f"{work / '.git'}\n")
    (work / ".git").write_text(f"gitdir: {admin}\n")
    assert git_dirs(work, [mirrors]) == (admin, repo)
    secrets = tmp_path / "config" / "worktrees" / "x"
    secrets.mkdir(parents=True)
    (secrets / "gitdir").write_text(f"{work / '.git'}\n")
    for named in (secrets, tmp_path / "config", repo / "worktrees" / "other"):
        (work / ".git").write_text(f"gitdir: {named}\n")
        assert git_dirs(work, [mirrors]) == (None, None), named
    other = repo / "worktrees" / "other"
    other.mkdir()
    (other / "gitdir").write_text(f"{tmp_path / 'elsewhere' / '.git'}\n")
    (work / ".git").write_text(f"gitdir: {other}\n")
    assert git_dirs(work, [mirrors]) == (None, None), "another checkout's"


def test_mount_points_in_the_runs_home_belong_to_the_service_user(
    tmp_path: Path,
) -> None:
    """``docker`` would create them, and the folders above them, as root: OpenCode
    then could not make ``~/.local/state`` beside a mounted
    ``~/.local/share/opencode``. A link an earlier attempt left is not followed."""
    home, run_dir = tmp_path / "home", tmp_path / "run"
    (home / ".local/share/opencode").mkdir(parents=True)
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run_dir / "home").mkdir(parents=True)
    (run_dir / "home/.config").symlink_to(elsewhere)
    binary = tmp_path / "opencode"
    binary.write_text("")
    box = ContainerSandbox("agent:1", home=home)
    box.prepare(
        "run_1",
        tmp_path,
        run_dir,
        str(binary),
        [str(binary), "acp"],
        {},
        home_paths=[".local/share/opencode", ".config/opencode", ".claude.json"],
    )
    inside = run_dir / "home"
    assert (inside / ".local/share/opencode").is_dir()
    assert (inside / ".claude.json").is_file()
    assert (inside / ".local").stat().st_uid == os.getuid()
    assert not (elsewhere / "opencode").exists(), "the link was not followed"


def test_only_a_reproductions_agent_gets_the_lab_socket(tmp_path: Path) -> None:
    from auto_maintainer.agents.container import LAB_SOCKET

    work, run_dir = tmp_path / "work", tmp_path / "run"
    work.mkdir()
    (run_dir / "bin").mkdir(parents=True)
    lab = tmp_path / "lab.sock"
    lab.write_text("")
    binary = tmp_path / "agy"
    binary.write_text("")
    box = ContainerSandbox("agent:1", home=tmp_path / "home")
    with_session = box.prepare(
        "run_1",
        work,
        run_dir,
        str(binary),
        [str(binary)],
        {"AM_LAB_SESSION": "t0ken", "AM_LAB_SOCKET": str(lab)},
    )
    mounts = _mounts(with_session.argv)
    assert mounts[LAB_SOCKET] == (str(lab), False)
    assert with_session.env["AM_LAB_SOCKET"] == LAB_SOCKET
    assert f"PATH={run_dir / 'bin'}:/opt/am/bin" in " ".join(with_session.argv)
    without = box.prepare(
        "run_2", work, run_dir, str(binary), [str(binary)], {"AM_LAB_SOCKET": str(lab)}
    )
    assert LAB_SOCKET not in _mounts(without.argv)
    assert "AM_LAB_SOCKET" not in without.env, "no session, no socket"


class FakeBox:
    """Runs the CLI on the host, recording what the runtime asked of the sandbox."""

    github_socket = Path("/nonexistent.sock")

    def __init__(self) -> None:
        self.prepared: list[dict[str, Any]] = []
        self.stopped: list[str] = []

    def prepare(self, run_id, workdir, run_dir, binary, argv, env, **kw) -> Prepared:
        self.prepared.append({"workdir": workdir, "argv": argv, **kw})
        return Prepared(argv=list(argv), env=dict(env), name=f"am-{run_id}")

    async def stop(self, name: str) -> None:
        self.stopped.append(name)


async def test_a_runtime_runs_each_task_in_its_container(
    tmp_path: Path, talking_cli
) -> None:
    cli = talking_cli(
        [
            {
                "wait": '"initialize"',
                "emit": [{"jsonrpc": "2.0", "id": 1, "result": {}}],
            },
            {
                "wait": '"session/new"',
                "emit": [{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s1"}}],
            },
            {
                "wait": '"session/prompt"',
                "emit": [
                    {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}
                ],
            },
        ]
    )
    box = FakeBox()
    runtime = AntigravityRuntime(binary=str(cli.path), runs_dir=tmp_path / "runs")
    runtime.container_sandbox = box
    task = AgentTask(
        kind=TaskKind.REVIEW,
        prompt="Review it.",
        workdir=tmp_path,
        skill_dirs=[tmp_path / "skills"],
        permissions="bypass",
    )
    events = [e async for e in runtime.run(task)]
    assert events[-1].type == AgentEventType.FINAL
    [asked] = box.prepared
    assert asked["home_paths"] == (".gemini",) and asked["ro"] == [tmp_path / "skills"]
    assert "--dangerously-skip-permissions" in asked["argv"], "everything is permitted"
    assert box.stopped == [f"am-{task.id}"], "the container goes when the run ends"
    [run_dir] = (tmp_path / "runs").glob(f"{task.id}-*")
    config = run_dir / "gh-config" / "config.yml"
    assert f"http_unix_socket: {SOCKET}" in config.read_text()


def test_inside_a_container_every_task_runs_in_bypass(tmp_path: Path) -> None:
    runtime = build_runtime("antigravity", RuntimeConfig(adapter="antigravity"))
    task = AgentTask(kind=TaskKind.REVIEW, prompt="x", workdir=tmp_path)
    assert effective_permissions(Route(primary="antigravity"), runtime, task) == "auto"
    runtime.container_sandbox = FakeBox()
    assert (
        effective_permissions(Route(primary="antigravity"), runtime, task) == "bypass"
    )


async def test_the_sandbox_is_given_to_runtimes_once_ready(tmp_path: Path) -> None:
    class ReadyBox(FakeBox):
        docker = "sh"
        image = "agent:1"

        async def ready(self) -> bool:
            return True

    runtime = build_runtime("antigravity", RuntimeConfig(adapter="antigravity"))
    box = ReadyBox()
    starter = SandboxStarter(box, lambda: {"antigravity": runtime})  # type: ignore[arg-type]
    later = build_runtime("antigravity", RuntimeConfig(adapter="antigravity"))
    starter.attach({"later": later})
    assert later.container_sandbox is None, "not before it is ready"
    await starter.start()
    await starter._task  # type: ignore[misc]
    assert (
        runtime.container_sandbox is box and "containers of agent:1" in starter.status
    )
    starter.attach({"later": later})
    assert later.container_sandbox is box, "runtimes built later get it too"

    missing = SandboxStarter(
        ContainerSandbox("agent:1", docker="no-such-docker"), lambda: {}
    )
    await missing.start()
    await missing._task  # type: ignore[misc]
    assert not missing.ready and "not installed" in missing.status


async def test_agent_containers_a_killed_service_left_are_removed(
    tmp_path: Path,
) -> None:
    """A service killed before it could stop its agents leaves their containers: the
    next start removes them, so they do not go on beside the agents that resume."""
    calls = tmp_path / "calls"
    docker = tmp_path / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\n'
        'if [ "$1" = ps ]; then printf "c1\\nc2\\n"; fi\n'
    )
    docker.chmod(0o755)
    box = ContainerSandbox("agent:1", docker=str(docker))
    assert await box.remove_leftovers() == 2
    assert calls.read_text().splitlines() == [
        "ps -aq --filter name=^am-run_",
        "rm -f c1 c2",
    ]


def test_only_a_conversations_agent_gets_the_records_door(tmp_path: Path) -> None:
    from auto_maintainer.agents.container import RECORDS_SOCKET

    work, run_dir = tmp_path / "work", tmp_path / "run"
    work.mkdir()
    (run_dir / "bin").mkdir(parents=True)
    door = tmp_path / "records.sock"
    door.write_text("")
    binary = tmp_path / "agy"
    binary.write_text("")
    box = ContainerSandbox("agent:1", home=tmp_path / "home")
    talking = box.prepare(
        "run_1",
        work,
        run_dir,
        str(binary),
        [str(binary)],
        {"AM_RECORDS_TOKEN": "t0ken", "AM_RECORDS_SOCKET": str(door)},
    )
    assert _mounts(talking.argv)[RECORDS_SOCKET] == (str(door), False)
    assert talking.env["AM_RECORDS_SOCKET"] == RECORDS_SOCKET
    assert "t0ken" not in " ".join(talking.argv), "the token is not on the command line"
    other = box.prepare(
        "run_2",
        work,
        run_dir,
        str(binary),
        [str(binary)],
        {"AM_RECORDS_SOCKET": str(door)},
    )
    assert RECORDS_SOCKET not in _mounts(other.argv)
    assert "AM_RECORDS_SOCKET" not in other.env, "no token, no door"


async def test_an_agents_code_runs_in_a_container_and_the_rest_on_the_host(
    tmp_path: Path,
) -> None:
    from auto_maintainer.agents.container import ContainedSandbox
    from auto_maintainer.domain.ports import CommandResult

    class Box:
        def __init__(self) -> None:
            self.prepared: list[dict[str, Any]] = []
            self.stopped: list[str] = []

        def prepare(self, name, workdir, run_dir, binary, argv, env, **kw):  # noqa: ANN001, ANN003
            self.prepared.append({"argv": argv, "env": env, "rw": kw.get("rw")})
            return Prepared(argv=["sh", "-c", "echo in-box"], env={}, name=f"am-{name}")

        async def stop(self, name: str) -> None:
            self.stopped.append(name)

    class Host:
        def __init__(self) -> None:
            self.ran: list[list[str]] = []

        async def run(self, argv, **kw):  # noqa: ANN001, ANN003
            self.ran.append(argv)
            return CommandResult(exit_code=0, stdout="on-host")

    class Starter:
        ready = enabled = True
        sandbox = Box()

    host = Host()
    sandbox = ContainedSandbox(Starter(), host, tmp_path / "sandbox")  # type: ignore[arg-type]
    tests = ["uv", "run", "pytest"]
    boxed = await sandbox.run(tests, cwd=tmp_path, untrusted=True)
    assert boxed.stdout.strip() == "in-box" and host.ran == []
    [prepared] = Starter.sandbox.prepared
    assert prepared["argv"] == tests
    assert prepared["env"]["UV_CACHE_DIR"] == str(tmp_path / "sandbox" / "uv-cache")
    assert Starter.sandbox.stopped and not list(
        (tmp_path / "sandbox" / "runs").iterdir()
    )
    script = ["scripts/check.sh"]
    assert (await sandbox.run(script, cwd=tmp_path)).stdout == "on-host"
    Starter.ready = False
    assert (await sandbox.run(tests, cwd=tmp_path, untrusted=True)).stdout == "on-host"
    assert host.ran == [script, tests], "no container on this host: as its agents do"


async def test_the_host_runs_an_agents_cli_only_in_a_container_too(
    tmp_path: Path,
) -> None:
    """An agent can write its CLI's home folders (settings, hooks, the programs the
    CLI runs), so a sign-in check, a version or model probe, and a sign-in run in a
    container too, never on the host with those folders."""
    from datetime import UTC, datetime

    from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
    from auto_maintainer.domain.agents import AgentLogin

    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho '{\"loggedIn\": true}'\n")
    cli.chmod(0o755)
    box = FakeBox()
    runtime = ClaudeCodeRuntime(binary=str(cli), runs_dir=tmp_path / "runs")
    runtime.container_sandbox = box  # type: ignore[assignment]
    assert await runtime.signed_in() is True
    assert (await runtime.probe()).available
    person = tmp_path / "people" / "walt" / "home"
    state = AgentLogin(agent="claude-code", by="walt", started_at=datetime.now(UTC))
    await runtime.start_login(person, state).wait()
    asked = [(p["argv"][1:], p.get("home_source")) for p in box.prepared]
    assert asked == [
        (["auth", "status"], None),
        (["--version"], None),
        (["auth", "login"], person),
    ]
    assert all(p["home_paths"] == (".claude", ".claude.json") for p in box.prepared)
    assert (person / ".claude").is_dir() and (person / ".claude.json").read_text()
    assert len(box.stopped) == 3, "each container goes when it is done"
    assert not list((tmp_path / "runs").glob("run_cli_*")), "and its folder"
