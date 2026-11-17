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

"""What a session leaves on a test machine (``labs/executor.py``): nothing it started
outlives it, its PR's lasting folder does, and the sweep removes what older sessions
left behind."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from auto_maintainer.domain.actions import LabReproducePayload
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.ports import CommandResult
from auto_maintainer.domain.profile import LabBinding
from auto_maintainer.domain.settings import LabMachine
from auto_maintainer.labs.executor import LabExecutor, tar_base64
from auto_maintainer.labs.hosts import LocalLabHost
from auto_maintainer.testing.fakes import FakeLabHost

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc"
)


def _tagged(value: str) -> list[int]:
    """The processes whose environment carries ``value``."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                if value.encode() in f.read().split(b"\0"):
                    found.append(int(entry))
        except OSError:
            continue
    return found


def _in_folder(folder: str) -> list[int]:
    """The processes whose working directory is in ``folder``."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        if cwd == folder or cwd.startswith(folder + "/"):
            found.append(int(entry))
    return found


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except OSError:
        return False


async def test_closing_a_session_stops_what_it_left_running(tmp_path: Path) -> None:
    from auto_maintainer.store.sqlite import SqliteStore

    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"local": LabMachine(workdir=str(tmp_path / "work"))})
    labs.hosts["local"] = LocalLabHost("local")
    source = tmp_path / "src"
    source.mkdir()
    (source / "README").write_text("x")
    opened = await labs.open_session(
        LabReproducePayload(lab="local", summary="s", sides=["head"]),
        "acme/widget",
        7,
        {"head": tar_base64(str(source), ["README"])},
        "act_abcdef12",
        binding=LabBinding(),
    )
    session = opened["session"]
    token = labs.session_token(session)
    assert token
    # A server left in the background, and one that clears its environment but
    # runs in the session's folder.
    ran = await labs.run_in_session(
        token,
        "nohup sleep 300 >/dev/null 2>&1 & "
        "env -i nohup sleep 301 >/dev/null 2>&1 & echo started",
    )
    assert ran["exit_code"] == 0
    await asyncio.sleep(0.3)
    tagged = _tagged(f"AM_LAB_RUN={session}")
    assert tagged, "what the session started carries its id"
    run_dir = str(tmp_path / "work" / "acme__widget-7-abcdef12")
    in_folder = [p for p in _in_folder(run_dir) if p not in tagged]
    assert in_folder, "the one that cleared its environment runs in the folder"
    keep = tmp_path / "work" / "keep" / "acme__widget-7"
    assert (keep / ".last_used").exists(), "the PR's lasting folder is made"
    (keep / "venv").mkdir()

    await labs.close_session(session)
    await asyncio.sleep(0.2)
    assert not any(_alive(p) for p in tagged + in_folder), "nothing outlives it"
    assert not Path(run_dir).exists()
    assert (keep / "venv").is_dir(), "the lasting folder stays for the next session"


async def test_the_sweep_removes_only_what_no_session_claims(tmp_path: Path) -> None:
    from auto_maintainer.store.sqlite import SqliteStore

    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"gpu-01": LabMachine(workdir="/data/am")})
    host = FakeLabHost("gpu-01")
    labs.hosts["gpu-01"] = host
    day = 86400
    host.results = [
        CommandResult(
            exit_code=0,
            stdout="\n".join(
                [
                    f"R acme__widget-3-aaaaaaaa {2 * day}",  # left over: removed
                    "R acme__widget-4-bbbbbbbb 600",  # young: may be another's
                    f"R venvs {30 * day}",  # not a session's folder
                    f"R keep {30 * day}",
                    "K acme__widget-5 3600",  # its PR closed: removed
                    "K acme__widget-6 3600",  # open and used lately: stays
                    f"K acme__widget-8 {8 * day}",  # unused a week: removed
                    f"P 4242 {2 * day} lab_gone",  # a gone session's server
                    "P 4343 60 lab_other",  # young: stays
                ]
            ),
        )
    ]

    async def still_open(repo: str, number: int) -> bool:
        return number != 5

    assert await labs.sweep(still_open) == 4
    ran = [c for c, _, _ in host.commands[1:]]
    assert ran[0].endswith("rm -rf /data/am/acme__widget-3-aaaaaaaa")
    assert "kill -TERM" in ran[0], "what ran in the folder is stopped first"
    assert ran[1] == "rm -rf /data/am/keep/acme__widget-5"
    assert ran[2] == "rm -rf /data/am/keep/acme__widget-8"
    assert ran[3].startswith("kill -KILL 4242")
    assert len(ran) == 4
