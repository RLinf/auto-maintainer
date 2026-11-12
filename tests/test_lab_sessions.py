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

"""An agent's ``lab`` command, through the session socket, to a test machine."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from auto_maintainer.agents.exec import LAB_CLI
from auto_maintainer.domain.actions import LabReproducePayload
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.ports import CommandResult
from auto_maintainer.domain.profile import LabBinding
from auto_maintainer.domain.settings import LabMachine
from auto_maintainer.labs.executor import LabExecutor
from auto_maintainer.labs.sessions import LabSessionServer
from auto_maintainer.testing.fakes import FakeLabHost


@pytest.fixture
async def door(tmp_path: Path):
    from auto_maintainer.store.sqlite import SqliteStore

    host = FakeLabHost("gpu-01")
    host.results = [
        CommandResult(exit_code=0, stdout="/home/ci"),
        CommandResult(exit_code=0),
    ]
    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"gpu-01": LabMachine(forbid=[r"\bdocker\b"])})
    labs.hosts["gpu-01"] = host
    # A Unix socket path must be short.
    folder = Path(tempfile.mkdtemp(prefix="am-lab-", dir="/tmp"))
    server = LabSessionServer(folder / "lab.sock", labs)
    await server.start()
    opened = await labs.open_session(
        LabReproducePayload(lab="gpu-01", summary="s", sides=["head"]),
        "acme/widget",
        42,
        {"head": ""},
        "act_12345678",
        binding=LabBinding(),
    )
    token = labs.session_token(opened["session"])
    yield host, server, token
    await server.stop()
    shutil.rmtree(folder, ignore_errors=True)


async def _lab(
    server: LabSessionServer, token: str, *args: str
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(LAB_CLI),
        *args,
        env={"AM_LAB_SESSION": token, "AM_LAB_SOCKET": str(server.socket_path)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(), err.decode()


async def test_lab_runs_a_command_on_the_machine_and_streams_its_output(door) -> None:
    host, server, token = door
    host.results = [CommandResult(exit_code=3, stdout="step 1\nstep 2")]
    code, out, _ = await _lab(server, token, "--", "make check")
    assert (code, out) == (3, "step 1\nstep 2\n")
    assert host.commands[-1][0].endswith("make check")
    assert host.commands[-1][1] == "/home/ci/am-labs/acme__widget-42-12345678/head"
    code, out, _ = await _lab(server, token, "--info")
    assert code == 0 and json.loads(out)["sides"] == ["head"]


async def test_lab_refuses_what_the_machine_forbids_and_unknown_tokens(door) -> None:
    host, server, token = door
    before = len(host.commands)
    code, _, err = await _lab(server, token, "--", "docker run x")
    assert code == 126 and "refused" in err
    code, _, err = await _lab(server, "not-a-token", "--", "ls")
    assert code == 1 and "no open session" in err
    code, _, err = await _lab(server, token, "--side", "base", "--", "ls")
    assert code == 1 and "no 'base' side" in err
    assert len(host.commands) == before, "nothing ran on the machine"


async def test_what_a_login_shell_prints_before_the_command_is_dropped() -> None:
    from auto_maintainer.labs.hosts import START, LocalLabHost, _AfterStart, after_start

    lines: list[str] = []

    async def seen(text: str) -> None:
        lines.append(text)

    kept = _AfterStart(seen)
    for text in [
        "=== welcome to the cluster ===",
        "PATH  /opt/bin",
        START,
        "ok",
        "done",
    ]:
        await kept.line(text)
    await kept.flush()
    assert lines == ["ok", "done"]
    assert after_start(f"banner\n{START}\nok\n") == "ok\n"
    assert after_start("no marker\n") == "no marker\n", "nothing is lost without it"

    lines.clear()
    result = await LocalLabHost("here").run("echo one; exit 3", on_output=seen)
    assert (result.exit_code, result.stdout, lines) == (3, "one\n", ["one"])


async def test_lab_takes_the_gpus_a_run_needs_only_when_they_are_idle(
    tmp_path: Path,
) -> None:
    """A session asks for more GPUs from inside it and gets them only all at once;
    the commands after it see every GPU it holds, and it can give some back."""
    from auto_maintainer.domain.ports import GpuStatus
    from auto_maintainer.store.sqlite import SqliteStore

    host = FakeLabHost(
        "gpu-01",
        gpus=[GpuStatus(index=i, memory_used_mib=0) for i in range(4)],
    )
    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"gpu-01": LabMachine()})
    labs.hosts["gpu-01"] = host
    folder = Path(tempfile.mkdtemp(prefix="am-lab-", dir="/tmp"))
    server = LabSessionServer(folder / "lab.sock", labs)
    await server.start()
    try:
        payload = LabReproducePayload(
            lab="gpu-01", summary="s", sides=["head"], wants_gpu=True
        )
        mine = await labs.open_session(
            payload,
            "acme/widget",
            42,
            {"head": ""},
            "act_mine0001",
            binding=LabBinding(),
        )
        other = await labs.open_session(
            payload,
            "acme/widget",
            43,
            {"head": ""},
            "act_other001",
            binding=LabBinding(),
        )
        assert (mine["gpus"], other["gpus"]) == ([0], [1])
        token = labs.session_token(mine["session"])
        assert token
        code, _, err = await _lab(server, token, "--gpus", "4")
        assert code == 1 and "not idle" in err, "three more are not free at once"
        assert labs.session_info(token)["gpus"] == [0], "it took none of them"
        code, out, _ = await _lab(server, token, "--gpus", "3")
        assert code == 0 and "holds 3 GPU(s): 0, 2, 3" in out
        await _lab(server, token, "--", "nvidia-smi")
        assert host.commands[-1][2]["CUDA_VISIBLE_DEVICES"] == "0,2,3"
        code, out, _ = await _lab(server, token, "--gpus", "1")
        assert code == 0 and "holds 1 GPU(s): 0" in out
        other_token = labs.session_token(other["session"])
        assert other_token
        code, out, _ = await _lab(server, other_token, "--gpus", "3")
        assert code == 0 and "1, 2, 3" in out, "what it gave back is free again"
    finally:
        await server.stop()
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ('grep -n "ray.shutdown\\|Shutting down" run.log | tail -5', []),
        ("grep -r 'pkill' docs/", []),
        ("python -c 'print(\"reboot\")'", []),
        ("rm -rf ./build /tmp/run", []),
        ("pytest tests && ray stop", ["stops every Ray job on the machine"]),
        ("sudo apt install x", ["uses sudo"]),
        ("echo $(sudo id)", ["uses sudo"]),
        ("if busy; then reboot; fi", ["shuts the machine down"]),
        ("pkill -f train.py", ["kills processes it did not start"]),
        ("rm -rf /", ["removes a root or home directory"]),
        ("systemctl restart docker", ["changes services or scheduled jobs"]),
    ],
)
def test_the_machine_refuses_a_command_only_where_it_runs(
    command: str, refused: list[str]
) -> None:
    """A word inside a search pattern or a string is no command: only one that runs
    is refused (two harmless reviews' commands were before)."""
    from auto_maintainer.domain.labs import script_refusals

    assert script_refusals(command) == refused
