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

"""Lab hosts reached locally or over SSH (design 5.10)."""

from __future__ import annotations

import shlex
from pathlib import Path

from ..domain.ports import CommandResult, GpuStatus
from ..subproc import run

GPU_QUERY = "nvidia-smi --query-gpu=index,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits"
APPS_QUERY = "nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader"


def parse_gpus(gpus_csv: str, apps_csv: str) -> list[GpuStatus]:
    per_uuid: dict[str, int] = {}
    for line in apps_csv.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if parts and parts[0]:
            per_uuid[parts[0]] = per_uuid.get(parts[0], 0) + 1
    out = []
    for line in gpus_csv.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        out.append(
            GpuStatus(
                index=int(parts[0]),
                memory_used_mib=int(float(parts[2])),
                memory_total_mib=int(float(parts[3])),
                utilization_pct=int(float(parts[4]))
                if parts[4].replace(".", "").isdigit()
                else 0,
                processes=per_uuid.get(parts[1], 0),
            )
        )
    return out


def _script(command: str, cwd: str | None, env: dict[str, str] | None) -> str:
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    return f"{prefix}{'env ' + exports + ' ' if exports else ''}bash -lc {shlex.quote(command)}"


class _Host:
    name: str

    async def _shell(
        self, script: str, timeout: float, stdin: str | None = None
    ) -> CommandResult:
        raise NotImplementedError

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        stdin: str | None = None,
    ) -> CommandResult:
        return await self._shell(_script(command, cwd, env), timeout, stdin)

    async def gpu_status(self) -> list[GpuStatus]:
        gpus = await self._shell(GPU_QUERY, 30)
        if gpus.exit_code != 0:
            return []
        apps = await self._shell(APPS_QUERY, 30)
        return parse_gpus(gpus.stdout, apps.stdout if apps.exit_code == 0 else "")


class LocalLabHost(_Host):
    def __init__(self, name: str) -> None:
        self.name = name

    async def _shell(
        self, script: str, timeout: float, stdin: str | None = None
    ) -> CommandResult:
        r = await run(["bash", "-c", script], timeout=timeout, stdin=stdin)
        return CommandResult(
            exit_code=r.exit_code,
            stdout=r.stdout,
            stderr=r.stderr,
            duration_seconds=r.duration_seconds,
            timed_out=r.timed_out,
        )


class SshLabHost(_Host):
    """One shared SSH connection per host (ControlMaster), batch mode only."""

    def __init__(self, name: str, host: str, control_dir: Path | None = None) -> None:
        self.name = name
        self.host = host
        self._control = str((control_dir or Path.home() / ".ssh") / "am-%r@%h:%p")

    async def _shell(
        self, script: str, timeout: float, stdin: str | None = None
    ) -> CommandResult:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self._control}",
            "-o",
            "ControlPersist=10m",
            "-o",
            "ServerAliveInterval=30",
            "--",
            self.host,
            script,
        ]
        r = await run(argv, timeout=timeout, stdin=stdin)
        return CommandResult(
            exit_code=r.exit_code,
            stdout=r.stdout,
            stderr=r.stderr,
            duration_seconds=r.duration_seconds,
            timed_out=r.timed_out,
        )
