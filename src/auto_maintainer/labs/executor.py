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

"""Run a lab template for a PR under the lab's safety rules (design 5.10).

Only the actuator calls this. Every run gets its own worktree, leases only idle
GPUs, passes the command guard, and cleans up whatever happens.
"""

from __future__ import annotations

import random
import re
import shlex
from typing import Any

from pydantic import BaseModel

from ..domain.actions import LabRunPayload
from ..domain.clock import Clock
from ..domain.ports import GpuStatus, LabHost, Store
from ..domain.profile import LabBinding, RepoProfile


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
