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

import asyncio
import base64
import contextlib
import io
import random
import re
import secrets
import shlex
import tarfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..domain.actions import LabReproducePayload, LabRunPayload
from ..domain.clock import Clock
from ..domain.ids import new_id
from ..domain.labs import (
    LabProgress,
    forbidden,
    script_refusals,
    setup_lines,
    time_limit,
    unusable,
)
from ..domain.ports import GpuStatus, LabHost, Store
from ..domain.profile import LabBinding, RepoProfile
from ..domain.secrets import redact_secrets
from ..domain.settings import LabMachine
from ..subproc import run
from .hosts import LocalLabHost, SshLabHost

TAIL_LINES = 150


SESSIONS_KEY = "lab-sessions:open"
"""Where open sessions are recorded, so a restart cleans up their run folders."""


async def checkout_files(source: str) -> tuple[list[str], list[str]]:
    """The files of a git checkout to copy (tracked and new, without what git
    ignores) and the tracked ones deleted from it."""
    listed = await run(
        ["git", "-C", source, "ls-files", "-z", "-c", "-o", "--exclude-standard"],
        timeout=120,
    )
    deleted = await run(["git", "-C", source, "ls-files", "-z", "-d"], timeout=120)
    gone = [p for p in deleted.stdout.split("\0") if p]
    missing = set(gone)
    files = sorted({p for p in listed.stdout.split("\0") if p and p not in missing})
    return files, gone


def tar_base64(source: str, files: list[str]) -> str:
    """``files`` under ``source`` as a gzipped tar, in base64, for ``tar -xzf``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in files:
            full = Path(source) / path
            if full.is_symlink() or full.is_file():
                tar.add(full, arcname=path, recursive=False)
    return base64.b64encode(buffer.getvalue()).decode()


@dataclass
class LabSession:
    """An approved reproduction's hold on a test machine: its run folder with a copy
    of each side's code, its GPU, and its time limit; the agent's commands run there
    through ``LabExecutor.run_in_session`` with ``token``."""

    id: str
    token: str
    lab: str
    repo: str
    number: int
    holder: str
    run_dir: str
    sides: list[str]
    gpu: int | None
    env: dict[str, str]
    before: list[str]
    forbid: list[str]
    deadline: datetime
    progress: Callable[[LabProgress], Awaitable[None]] | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    source: str | None = None
    """The checkout on the service host that the agent edits, when it changes the
    code (fixing CI): ``sync_session`` copies it to the head side."""

    async def tell(
        self, side: str | None = None, phase: str = "", line: str = ""
    ) -> None:
        if self.progress is not None:
            with contextlib.suppress(Exception):
                await self.progress(LabProgress(side=side, phase=phase, line=line))


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
        self._sessions: dict[str, LabSession] = {}
        self._opening: dict[str, LabSession] = {}
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
                SshLabHost(
                    name,
                    machine.host or name,
                    user=machine.user,
                    port=machine.port,
                    proxy_jump=machine.proxy_jump,
                    identity_file=machine.identity_file,
                )
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

    async def open_session(
        self,
        payload: LabReproducePayload,
        repo: str,
        number: int,
        code: dict[str, str],
        holder: str,
        binding: LabBinding | None = None,
        progress: Callable[[LabProgress], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Open an approved reproduction's session: lease a GPU when it needs one,
        copy each side's code (base64 tar.gz) into a run folder of its own on the
        machine, and keep the machine for the agent until ``close_session`` or the
        time limit. Returns the session's id (its token stays in memory)."""
        machine = self.machines.get(payload.lab)
        host = self.hosts.get(payload.lab)
        why = unusable(repo, payload.lab, machine, binding)
        if why or machine is None:
            raise RuntimeError(why)
        if host is None:
            raise RuntimeError(f"there is no test machine named {payload.lab!r}")

        async def tell(
            side: str | None = None, phase: str = "", line: str = ""
        ) -> None:
            if progress is not None:
                with contextlib.suppress(Exception):
                    await progress(LabProgress(side=side, phase=phase, line=line))

        home = await host.run('printf %s "$HOME"', timeout=60)
        root = machine.workdir.rstrip("/")
        if not root.startswith("/"):
            if home.exit_code != 0 or not home.stdout.strip():
                raise RuntimeError(f"cannot find the home folder on {payload.lab}")
            root = f"{home.stdout.strip()}/{root}"
        run_dir = f"{root}/{repo.replace('/', '__')}-{number}-{holder[-8:]}"
        limit = min(payload.timeout_seconds, time_limit(machine, binding))
        env: dict[str, str] = {}
        gpu: int | None = None
        if payload.needs_gpu or payload.wants_gpu:
            await tell(phase=f"Waiting for an idle GPU on {payload.lab}")
            gpu = await self._lease_gpu(payload.lab, host, holder, limit)
            if gpu is None and payload.needs_gpu:
                return {
                    "lab": payload.lab,
                    "deferred": True,
                    "reason": "no idle GPU on the machine; nothing ran",
                }
            if gpu is None:
                await tell(phase="No idle GPU; the session runs without one")
            else:
                await tell(phase=f"Leased GPU {gpu}")
            # Without a lease, the GPUs stay hidden: they are other people's work.
            env["CUDA_VISIBLE_DEVICES"] = "" if gpu is None else str(gpu)
        session = LabSession(
            id=new_id("lab"),
            token=secrets.token_urlsafe(32),
            lab=payload.lab,
            repo=repo,
            number=number,
            holder=holder,
            run_dir=run_dir,
            sides=list(payload.sides),
            gpu=gpu,
            env=env,
            before=setup_lines(machine, binding),
            forbid=forbidden(machine, binding),
            deadline=self.clock.now() + timedelta(seconds=limit),
            progress=progress,
        )
        await self._remember(session)
        for side in payload.sides:
            await tell(side, phase=f"Copying the {side} code to {payload.lab}")
            put = await host.run(
                f"mkdir -p {shlex.quote(run_dir + '/' + side)} && base64 -d "
                f"| tar -xzf - -C {shlex.quote(run_dir + '/' + side)}",
                timeout=900,
                stdin=code[side],
            )
            if put.exit_code != 0:
                await self.close_session(session.id)
                return {
                    "lab": payload.lab,
                    "error": f"could not copy the {side} code to the machine",
                    "tail": _tail(put.stdout + "\n" + put.stderr),
                }
        self._sessions[session.id] = session
        self._opening.pop(session.id, None)
        await tell(
            phase=f"The agent works on {payload.lab} for at most "
            f"{round(limit / 60)} minutes"
        )
        return {
            "lab": payload.lab,
            "session": session.id,
            "gpu": gpu,
            "sides": session.sides,
            "expires_at": session.deadline.isoformat(),
        }

    def session_token(self, session_id: str) -> str | None:
        """The secret the agent of ``session_id`` sends with each command."""
        session = self._sessions.get(session_id)
        return session.token if session and not session.closed else None

    def session_info(self, token: str) -> dict[str, Any] | None:
        """What the agent may know about its session."""
        session = self._by_token(token)
        if session is None:
            return None
        left = (session.deadline - self.clock.now()).total_seconds()
        return {
            "lab": session.lab,
            "sides": session.sides,
            "gpu": session.gpu,
            "seconds_left": max(0, int(left)),
            "run_dir": session.run_dir,
        }

    async def run_in_session(
        self,
        token: str,
        command: str,
        *,
        side: str = "head",
        timeout_seconds: int | None = None,
        on_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Run one of the agent's commands in its session: in a fresh shell in the
        side's folder, after the machine's and the repository's setup, once the
        command passes the machine's rules, within the session's time. Each line of
        output goes to ``on_line`` and to the job's live view as it comes."""
        session = self._by_token(token)
        if session is None:
            return {"error": "there is no open session for this token"}
        if side not in session.sides:
            return {"error": f"this session has no {side!r} side: {session.sides}"}
        left = (session.deadline - self.clock.now()).total_seconds()
        if left <= 0:
            return {"error": "the session's time is up"}
        refused = script_refusals(command, session.forbid)
        if refused:
            entry = {"command": command, "side": side, "refused": refused}
            session.log.append(entry)
            await session.tell(side, phase=f"Refused: {command[:200]}")
            return {"exit_code": 126, "refused": refused}
        host = self.hosts.get(session.lab)
        if host is None:
            return {"error": f"there is no test machine named {session.lab!r}"}
        timeout = min(left, timeout_seconds or left)
        merged: deque[str] = deque(maxlen=TAIL_LINES)

        async def output(line: str) -> None:
            merged.append(line)
            await session.tell(side, line=line)
            if on_line is not None:
                with contextlib.suppress(Exception):
                    await on_line(line)

        async with session.lock:
            await session.tell(side, phase=f"$ {command[:300]}")
            result = await host.run(
                "\n".join([*session.before, command]),
                cwd=f"{session.run_dir}/{side}",
                env=session.env,
                timeout=timeout,
                on_output=output,
            )
        outcome = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 1),
        }
        session.log.append(
            {
                "command": command,
                "side": side,
                **outcome,
                "tail": _tail("\n".join(merged)),
            }
        )
        await session.tell(
            side,
            phase=("timed out" if result.timed_out else f"exit code {result.exit_code}")
            + f" after {round(result.duration_seconds)} s",
        )
        return outcome

    def allow_sync(self, session_id: str, source: str) -> None:
        """Let the session's agent copy ``source``, the checkout it edits, to the
        machine's head side (``lab --sync``)."""
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            session.source = source

    async def sync_session(self, token: str, side: str = "head") -> dict[str, Any]:
        """Copy the agent's checkout to the side's folder on the machine: its tracked
        and new files (not what git ignores), and remove the files it deleted, so the
        side runs the code as the agent left it. Environments and caches the agent
        made inside the folder stay."""
        session = self._by_token(token)
        if session is None:
            return {"error": "there is no open session for this token"}
        if side not in session.sides:
            return {"error": f"this session has no {side!r} side: {session.sides}"}
        if not session.source:
            return {"error": "this session has no checkout to copy; its code is fixed"}
        left = (session.deadline - self.clock.now()).total_seconds()
        if left <= 0:
            return {"error": "the session's time is up"}
        host = self.hosts.get(session.lab)
        if host is None:
            return {"error": f"there is no test machine named {session.lab!r}"}
        files, gone = await checkout_files(session.source)
        archive = await asyncio.to_thread(tar_base64, session.source, files)
        folder = f"{session.run_dir}/{side}"
        removal = (
            "rm -f -- " + " ".join(shlex.quote(p) for p in gone) + " && "
            if gone
            else ""
        )
        async with session.lock:
            await session.tell(side, phase=f"Copying the agent's changes to {side}")
            put = await host.run(
                f"cd {shlex.quote(folder)} && {removal}base64 -d | tar -xzf -",
                timeout=min(left, 900),
                stdin=archive,
            )
        outcome: dict[str, Any] = {
            "exit_code": put.exit_code,
            "files": len(files),
            "removed": len(gone),
        }
        if put.exit_code != 0:
            outcome["error"] = "could not copy the checkout to the machine: " + _tail(
                put.stdout + "\n" + put.stderr
            )
        session.log.append({"command": "lab --sync", "side": side, **outcome})
        await session.tell(
            side,
            phase=f"Copied {len(files)} files"
            + (f", removed {len(gone)}" if gone else ""),
        )
        return outcome

    async def close_session(self, session_id: str) -> dict[str, Any] | None:
        """End a session: remove its run folder and release its GPU. Returns what
        ran in it; ``None`` when there is no such session."""
        session = self._sessions.pop(session_id, None) or self._opening.pop(
            session_id, None
        )
        if session is None:
            return None
        self._opening.pop(session_id, None)
        if not session.closed:
            session.closed = True
            await session.tell(phase=f"Removing the run folder on {session.lab}")
            await self._clean(session.lab, session.run_dir, session.gpu, session.holder)
            await self._forget(session_id)
        return {"lab": session.lab, "gpu": session.gpu, "commands": session.log}

    async def expire_sessions(self) -> int:
        """Close the sessions past their time limit; how many."""
        now = self.clock.now()
        late = [s.id for s in self._sessions.values() if s.deadline <= now]
        for session_id in late:
            await self.close_session(session_id)
        return len(late)

    async def recover_sessions(self) -> int:
        """After a restart: clean up the sessions the last process left open (their
        agents are gone); how many."""
        left = await self.store.get_state(SESSIONS_KEY) or {}
        for record in left.values():
            with contextlib.suppress(Exception):
                await self._clean(
                    record["lab"],
                    record["run_dir"],
                    record.get("gpu"),
                    record["holder"],
                )
        await self.store.set_state(SESSIONS_KEY, {})
        return len(left)

    def _by_token(self, token: str) -> LabSession | None:
        for session in self._sessions.values():
            if not session.closed and secrets.compare_digest(session.token, token):
                return session
        return None

    async def _clean(
        self, lab: str, run_dir: str, gpu: int | None, holder: str
    ) -> None:
        host = self.hosts.get(lab)
        if host is not None:
            with contextlib.suppress(Exception):
                await host.run(f"rm -rf {shlex.quote(run_dir)}", timeout=300)
        await self._release(lab, gpu, holder)

    async def _remember(self, session: LabSession) -> None:
        self._opening[session.id] = session
        open_ = await self.store.get_state(SESSIONS_KEY) or {}
        open_[session.id] = {
            "lab": session.lab,
            "run_dir": session.run_dir,
            "gpu": session.gpu,
            "holder": session.holder,
        }
        await self.store.set_state(SESSIONS_KEY, open_)

    async def _forget(self, session_id: str) -> None:
        open_ = await self.store.get_state(SESSIONS_KEY) or {}
        open_.pop(session_id, None)
        await self.store.set_state(SESSIONS_KEY, open_)

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
