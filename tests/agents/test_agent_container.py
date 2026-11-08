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
    config = tmp_path / "runs" / task.id / "gh-config" / "config.yml"
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
