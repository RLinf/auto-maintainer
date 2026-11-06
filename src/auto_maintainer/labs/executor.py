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

"""Run a lab template, or an approved reproduction, for a PR under the lab's safety
rules (design 5.10).

Only the actuator calls this. Every run gets its own worktree or run folder, leases
only idle GPUs, passes the command guard, and cleans up whatever happens.
"""

from __future__ import annotations

import random
import re
import shlex
from typing import Any

from pydantic import BaseModel

from ..domain.actions import LabReproducePayload, LabRunPayload
from ..domain.clock import Clock
from ..domain.labs import script_refusals
from ..domain.ports import GpuStatus, LabHost, Store
from ..domain.profile import LabBinding, RepoProfile
from ..domain.secrets import redact_secrets
from ..domain.settings import LabMachine
from .hosts import LocalLabHost, SshLabHost

TAIL_LINES = 80


class GpuThresholds(BaseModel):
    max_mem_mib: int = 500
    max_util_pct: int = 0


def is_idle(gpu: GpuStatus, t: GpuThresholds) -> bool:
    return (
        gpu.memory_used_mib < t.max_mem_mib
        and gpu.utilization_pct <= t.max_util_pct
        and gpu.processes == 0
    )


def guard(
    command: str, binding: LabBinding, worktree: str, throwaway: str | None
) -> list[str]:
    """Reasons to refuse ``command``; empty when it may run."""
    errors = []
    for pattern in binding.guard.forbid:
        if re.search(pattern, command):
            errors.append(f"matches the forbidden pattern {pattern!r}")
    if binding.base_clone and binding.base_clone in command:
        errors.append("touches the base clone; tests run in the PR worktree")
    for path in binding.protected_paths:
        if path and path in command:
            errors.append(f"touches the protected path {path}")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [*errors, "cannot be parsed as a shell command"]
    allowed = [worktree] + ([throwaway] if throwaway else [])
    for i, tok in enumerate(tokens):
        if tok != "rm":
            continue
        for target in tokens[i + 1 :]:
            if target in (";", "&&", "||", "|"):
                break
            if target.startswith("-"):
                continue
            if ".." in target.split("/") or (
                target.startswith("/")
                and not any(target.startswith(a.rstrip("/") + "/") for a in allowed)
            ):
                errors.append(
                    f"removes {target}, outside the PR worktree and throwaway environments"
                )
    return errors


class LabExecutor:
    def __init__(
        self,
        hosts: dict[str, LabHost],
        thresholds: dict[str, GpuThresholds],
        store: Store,
        clock: Clock,
    ) -> None:
        self.hosts = hosts
        self.thresholds = thresholds
        self.store = store
        self.clock = clock
        self.machines: dict[str, LabMachine] = {}
        self._fixed = dict(hosts)
        self._fixed_thresholds = dict(thresholds)

    def configure(self, machines: dict[str, LabMachine]) -> None:
        """Use these test machines from the service settings from now on, besides the
        hosts the host config declares. ``hosts`` is changed in place, so everyone
        holding it sees the new set."""
        self.hosts.clear()
        self.hosts.update(self._fixed)
        self.thresholds.clear()
        self.thresholds.update(self._fixed_thresholds)
        for name, machine in machines.items():
            self.hosts[name] = (
                SshLabHost(name, machine.host or name)
                if machine.transport == "ssh"
                else LocalLabHost(name)
            )
            self.thresholds[name] = GpuThresholds(
                max_mem_mib=machine.max_gpu_memory_mib,
                max_util_pct=machine.max_gpu_util_pct,
            )
        self.machines = dict(machines)

    async def check(self, lab: str) -> dict[str, Any]:
        """Whether the machine answers, what it is, and its GPUs."""
        host = self.hosts.get(lab)
        if host is None:
            raise RuntimeError(f"there is no test machine named {lab!r}")
        about = await host.run(
            "uname -srm; nproc; free -g | awk '/Mem:/{print $2}'", timeout=60
        )
        if about.exit_code != 0:
            return {
                "ok": False,
                "error": (about.stderr or about.stdout).strip()[-500:]
                or f"exit code {about.exit_code}",
            }
        lines = about.stdout.strip().splitlines()
        gpus = await host.gpu_status()
        thresholds = self.thresholds.get(lab, GpuThresholds())
        return {
            "ok": True,
            "system": lines[0] if lines else "",
            "cpus": lines[1] if len(lines) > 1 else "",
            "memory_gb": lines[2] if len(lines) > 2 else "",
            "gpus": [
                {**g.model_dump(mode="json"), "idle": is_idle(g, thresholds)}
                for g in gpus
            ],
        }

    async def reproduce(
        self,
        payload: LabReproducePayload,
        repo: str,
        number: int,
        code: dict[str, str],
        holder: str,
    ) -> dict[str, Any]:
        """Run an approved reproduction: copy each side's code (base64 tar.gz) into a
        run folder of its own on the machine, run the machine's setup, the script's
        setup, and the script there, and remove the folder whatever happens."""
        machine = self.machines.get(payload.lab, LabMachine())
        host = self.hosts.get(payload.lab)
        if host is None:
            raise RuntimeError(f"there is no test machine named {payload.lab!r}")
        if not machine.serves(repo):
            raise RuntimeError(f"test machine {payload.lab!r} is not for {repo}")
        steps = "\n".join(
            x for x in (machine.setup, payload.setup, payload.script) if x
        )
        refused = script_refusals(steps, machine.forbid)
        if refused:
            return {"lab": payload.lab, "refused": refused}
        home = await host.run('printf %s "$HOME"', timeout=60)
        root = machine.workdir.rstrip("/")
        if not root.startswith("/"):
            if home.exit_code != 0 or not home.stdout.strip():
                raise RuntimeError(f"cannot find the home folder on {payload.lab}")
            root = f"{home.stdout.strip()}/{root}"
        run_dir = f"{root}/{repo.replace('/', '__')}-{number}-{holder[-8:]}"
        env: dict[str, str] = {}
        gpu: int | None = None
        timeout = min(payload.timeout_seconds, machine.timeout_seconds)
        if payload.needs_gpu:
            gpu = await self._lease_gpu(payload.lab, host, holder, timeout)
            if gpu is None:
                return {
                    "lab": payload.lab,
                    "deferred": True,
                    "reason": "no idle GPU on the machine; nothing ran",
                }
            env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        runs: dict[str, Any] = {}
        try:
            for side in payload.sides:
                where = f"{run_dir}/{side}"
                put = await host.run(
                    f"mkdir -p {shlex.quote(where)} && base64 -d | tar -xzf - -C {shlex.quote(where)}",
                    timeout=900,
                    stdin=code[side],
                )
                if put.exit_code != 0:
                    runs[side] = {
                        "error": "could not copy the code to the machine",
                        "tail": _tail(put.stdout + "\n" + put.stderr),
                    }
                    continue
                result = await host.run(steps, cwd=where, env=env, timeout=timeout)
                runs[side] = {
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": round(result.duration_seconds, 1),
                    "tail": _tail(result.stdout + "\n" + result.stderr),
                }
        finally:
            await host.run(f"rm -rf {shlex.quote(run_dir)}", timeout=300)
            await self._release(payload.lab, gpu, holder)
        return {"lab": payload.lab, "gpu": gpu, "runs": runs}

    async def run(
        self,
        profile: RepoProfile,
        payload: LabRunPayload,
        repo: str,
        number: int | None,
        sha: str | None,
        holder: str,
    ) -> dict[str, Any]:
        binding = profile.labs.get(payload.lab)
        host = self.hosts.get(payload.lab)
        if binding is None or not binding.use or host is None:
            raise RuntimeError(f"lab {payload.lab!r} is not available for {repo}")
        template = binding.templates.get(payload.template)
        if template is None:
            raise RuntimeError(
                f"lab {payload.lab!r} has no template {payload.template!r}"
            )
        if not (binding.base_clone and binding.worktree_root and number and sha):
            raise RuntimeError(
                "lab runs need base_clone, worktree_root, a PR number, and a head SHA"
            )
        worktree = f"{binding.worktree_root.rstrip('/')}/pr-{number}"
        throwaway = (
            f"{binding.throwaway_env_root.rstrip('/')}/pr-{number}"
            if binding.throwaway_env_root
            else None
        )
        env: dict[str, str] = {}
        command_template = template.run
        gpu: int | None = None
        deferred = False
        if template.needs_gpu and not payload.cpu_only:
            gpu = await self._lease_gpu(
                payload.lab, host, holder, template.timeout_seconds
            )
            if gpu is None:
                if not template.cpu_fallback:
                    return {
                        "passed": False,
                        "deferred": True,
                        "reason": "no idle GPU on the lab; GPU tests were deferred",
                    }
                command_template, deferred = template.cpu_fallback, True
                env["CUDA_VISIBLE_DEVICES"] = ""
            else:
                env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        elif template.needs_gpu:
            command_template = template.cpu_fallback or template.run
            env["CUDA_VISIBLE_DEVICES"] = ""
        args = {
            **payload.args,
            "worktree": worktree,
            "number": str(number),
            "sha": sha,
            "throwaway_env": throwaway or "",
        }
        command = command_template.format(**args)
        refused = guard(command, binding, worktree, throwaway)
        if refused:
            await self._release(payload.lab, gpu, holder)
            return {"passed": False, "refused": refused}
        if binding.guard.ray_private_instance:
            env["AM_RAY_TEMP_DIR"] = f"/tmp/ray-pr-{number}"
            env["AM_RAY_PORT"] = str(random.randint(20000, 40000))
            env.pop("RAY_ADDRESS", None)
        base = shlex.quote(binding.base_clone)
        wt = shlex.quote(worktree)
        try:
            setup = await host.run(
                f"git -C {base} fetch -q origin pull/{number}/head && git -C {base} worktree add -f --detach {wt} {shlex.quote(sha)}",
                timeout=900,
            )
            if setup.exit_code != 0:
                return {
                    "passed": False,
                    "error": "could not create the PR worktree",
                    "tail": setup.stderr[-2000:],
                }
            full = f"{binding.setup} && {command}" if binding.setup else command
            result = await host.run(
                full, cwd=worktree, env=env, timeout=template.timeout_seconds
            )
            tail = (result.stdout + "\n" + result.stderr).strip().splitlines()[-40:]
            return {
                "passed": result.exit_code == 0 and not result.timed_out,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration_seconds, 1),
                "gpu": gpu,
                "deferred": deferred,
                "template": payload.template,
                "tail": tail,
            }
        finally:
            cleanup = (
                f"git -C {base} worktree remove --force {wt} 2>/dev/null; rm -rf {wt}"
            )
            if throwaway:
                cleanup += f"; rm -rf {shlex.quote(throwaway)}"
            await host.run(cleanup, timeout=300)
            await self._release(payload.lab, gpu, holder)

    async def _lease_gpu(
        self, lab: str, host: LabHost, holder: str, ttl: int
    ) -> int | None:
        thresholds = self.thresholds.get(lab, GpuThresholds())
        for gpu in [g for g in await host.gpu_status() if is_idle(g, thresholds)]:
            if await self.store.acquire_lease(
                lab, f"gpu{gpu.index}", holder, ttl + 600, self.clock.now()
            ):
                again = {g.index: g for g in await host.gpu_status()}
                if gpu.index in again and is_idle(again[gpu.index], thresholds):
                    return gpu.index
                await self.store.release_lease(lab, f"gpu{gpu.index}", holder)
        return None

    async def _release(self, lab: str, gpu: int | None, holder: str) -> None:
        if gpu is not None:
            await self.store.release_lease(lab, f"gpu{gpu}", holder)


def _tail(text: str) -> list[str]:
    """The last lines of a run's output, without anything that looks like a secret."""
    text, _ = redact_secrets(text)
    return [line[:400] for line in text.strip().splitlines()[-TAIL_LINES:]]
