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

"""Async subprocesses with timeouts. Adapters run every external command through ``run``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ProcError(RuntimeError):
    def __init__(self, argv: Sequence[str], result: ProcResult) -> None:
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        super().__init__(
            f"{argv[0]} exited with {result.exit_code}: {' | '.join(tail)}"
        )
        self.result = result


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = 600,
    check: bool = False,
) -> ProcResult:
    """Run ``argv`` (no shell) in its own process group; kill the group on timeout."""
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE
        if stdin is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = await proc.communicate()
    result = ProcResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        duration_seconds=time.monotonic() - start,
        timed_out=timed_out,
    )
    if check and not result.ok:
        raise ProcError(argv, result)
    return result
