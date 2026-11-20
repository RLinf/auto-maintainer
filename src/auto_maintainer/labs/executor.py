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
from ..domain.snapshot import PrState
from ..subproc import run
from .hosts import LocalLabHost, SshLabHost

TAIL_LINES = 150

GPU_POLL_SECONDS = 30
"""How often a session waiting for more GPUs (``grow_gpus``) looks again."""

MAX_SESSION_SECONDS = 6 * 3600
"""Hard wall-clock ceiling for an active session whose deadline slides while commands
run (e.g. large checkpoint/dataset downloads followed by GPU training)."""

SLIDE_SAVE_INTERVAL = 30.0
"""Minimum seconds between persisting sliding deadline updates while output streams."""


SESSIONS_KEY = "lab-sessions:open"
"""Where open sessions are recorded, so a restart cleans up their run folders."""

RUN_TAG = "AM_LAB_RUN"
"""Set to the session's id in every command a session runs on the machine. Whatever
the command starts inherits it, servers left in the background too, so closing the
session stops them all (``STOP``). Never the session's token, which stays with the
agent."""

KEEP_VAR = "AM_LAB_KEEP"
"""Set in every command to the PR's lasting folder (``keep_path``)."""

HEAD_VAR = "AM_LAB_HEAD"
"""Set in every command to the session's copy of the PR's head, when present."""

BASE_VAR = "AM_LAB_BASE"
"""Set in every command to the session's copy of the base branch, when present."""

_RESTORE_INSTRUCTIONS_SH = r"""
set -e
wt=%(wt)s
ref=%(ref)s
head_files=$(git -C "$wt" ls-tree -r --name-only HEAD)
base_files=$(git -C "$wt" ls-tree -r --name-only "$ref" 2>/dev/null || true)
is_inst() {
  case "$1" in
    AGENTS.md|*/AGENTS.md|CLAUDE.md|*/CLAUDE.md|GEMINI.md|*/GEMINI.md|.cursorrules|*/.cursorrules|copilot-instructions.md|*/copilot-instructions.md) return 0 ;;
    .claude/*|*/.claude/*|.agents/*|*/.agents/*|.codex/*|*/.codex/*|.cursor/*|*/.cursor/*|.gemini/*|*/.gemini/*|.opencode/*|*/.opencode/*) return 0 ;;
  esac
  return 1
}
printf '%%s\n' "$head_files" | while IFS= read -r p; do
  if [ -n "$p" ] && is_inst "$p"; then rm -f "$wt/$p"; fi
done
printf '%%s\n' "$base_files" | while IFS= read -r p; do
  if [ -n "$p" ] && is_inst "$p"; then printf '%%s\0' "$p"; fi
done | xargs -0 -r git -C "$wt" checkout -q "$ref" --
"""

KEEP_FOLDER = "keep"
"""Under a machine's work folder: one folder per PR, where its sessions keep
environments, downloaded checkpoints, and caches. The code folders are fresh each
session; this one is shared by the PR's later sessions until the PR closes or goes
``KEEP_DAYS`` without a session (``LabExecutor.sweep``)."""

KEEP_MARK = ".last_used"
"""Touched in a PR's lasting folder each time a session of the PR opens."""

KEEP_DAYS = 7

STRAY_HOURS = 24
"""Older than any session lasts: a run folder or a tagged process this old that no
open session claims is left over (a crash, an older version), and the sweep removes
it. Younger ones may belong to another service instance using the same machine."""

SWEEP_SECONDS = 3600
"""How often ``LabExecutor.sweep`` looks over the machines."""

RUN_FOLDER = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+-[A-Za-z0-9_]{8}$")
"""The name of a session's run folder (``open_session``)."""

PR_FOLDER = re.compile(r"^([A-Za-z0-9_.-]+?)__([A-Za-z0-9_.-]+)-(\d+)$")
"""The name of a PR's lasting folder: ``owner__name-number``."""

STOP = """tag=%(tag)s
dir=%(dir)s
me=$$
pids=""
for p in /proc/[0-9]*; do
  pid=${p#/proc/}
  [ "$pid" = "$me" ] && continue
  if [ -n "$tag" ] && grep -qzx -- "$tag" "$p/environ" 2>/dev/null; then
    pids="$pids $pid"; continue
  fi
  cwd=$(readlink "$p/cwd" 2>/dev/null) || continue
  case "$cwd" in "$dir"|"$dir"/*) pids="$pids $pid";; esac
done
if [ -n "$pids" ]; then
  kill -TERM $pids 2>/dev/null
  sleep 5
  kill -KILL $pids 2>/dev/null
fi
printf 'stopped %%s\n' "$(echo $pids | wc -w)"
"""
"""Stop every process of a session: those carrying its tag, and those running in its
run folder (from before the tag, or that cleared their environment)."""

SURVEY = """root=%(root)s
for d in "$root"/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d"); printf 'R %%s %%s\n' "$n" $(( $(date +%%s) - $(stat -c %%Y "$d") ))
done
for d in "$root"/%(keep)s/*/; do
  [ -d "$d" ] || continue
  m="$d%(mark)s"; [ -e "$m" ] || m="$d"
  printf 'K %%s %%s\n' "$(basename "$d")" $(( $(date +%%s) - $(stat -c %%Y "$m") ))
done
for p in /proc/[0-9]*; do
  t=$(tr '\0' '\n' < "$p/environ" 2>/dev/null | sed -n 's/^%(tag)s=//p' | head -1)
  [ -n "$t" ] || continue
  printf 'P %%s %%s %%s\n' "${p#/proc/}" "$(ps -o etimes= -p "${p#/proc/}" | tr -d ' ')" "$t"
done
"""
"""What the sweep looks at on a machine: the folders under its work folder, the PRs'
lasting folders with how long since a session used them, and the processes carrying
a session's tag with their age, one line each."""


def keep_path(root: str, repo: str, number: int) -> str:
    """A PR's lasting folder on a machine whose work folder is ``root``."""
    return f"{root}/{KEEP_FOLDER}/{repo.replace('/', '__')}-{number}"


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
    gpus: list[int]
    """The GPUs it leased; its commands see these and no others."""
    env: dict[str, str]
    before: list[str]
    forbid: list[str]
    deadline: datetime
    idle_limit: int = 1800
    """Seconds the session stays alive after its last command activity."""
    hard_deadline: datetime | None = None
    """Absolute ceiling beyond which sliding activity extensions stop."""
    active_commands: int = 0
    """Number of commands currently running in this session."""
    progress: Callable[[LabProgress], Awaitable[None]] | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    source: str | None = None
    """The checkout on the service host that the agent edits, when it changes the
    code (fixing CI): ``sync_session`` copies it to the head side."""
    job: str | None = None
    """The job whose agent works in the session, for its live view after a restart."""
    ready: bool = False
    """Its code is on the machine; a session a restart finds before that is removed."""
    worktree: str | None = None
    """Real git worktree of the PR head on the test machine, when created from a mirror."""
    mirror: str | None = None
    """Bare git mirror on the test machine backing ``worktree``."""

    @property
    def gpu(self) -> int | None:
        """Its first GPU, for those who know a session by one."""
        return self.gpus[0] if self.gpus else None

    def slide(self, now: datetime) -> None:
        """Extend the inactivity deadline on command activity up to ``hard_deadline``."""
        cap = self.hard_deadline or self.deadline
        self.deadline = min(
            cap, max(self.deadline, now + timedelta(seconds=self.idle_limit))
        )

    def record(self) -> dict[str, Any]:
        """What survives a restart: everything but its token and its callbacks."""
        return {
            "id": self.id,
            "lab": self.lab,
            "repo": self.repo,
            "number": self.number,
            "holder": self.holder,
            "run_dir": self.run_dir,
            "sides": self.sides,
            "gpus": self.gpus,
            "gpu": self.gpu,
            "env": self.env,
            "before": self.before,
            "forbid": self.forbid,
            "deadline": self.deadline.isoformat(),
            "idle_limit": self.idle_limit,
            "hard_deadline": (self.hard_deadline or self.deadline).isoformat(),
            "job": self.job,
            "ready": self.ready,
            "worktree": self.worktree,
            "mirror": self.mirror,
            "log": [{k: v for k, v in e.items() if k != "tail"} for e in self.log],
        }

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
        self.progress_sink: (
            Callable[[str, str, int | None, str, LabProgress], Awaitable[None]] | None
        ) = None
        """Where an adopted session's output goes (the job's live view), as the
        actuator sends it for the sessions it opens."""
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
            if machine.owner:
                # A person's own: their key and config alone, their own connections.
                if machine.transport != "ssh" or not machine.ssh_config:
                    continue
                self.hosts[name] = SshLabHost(
                    name,
                    machine.host or name,
                    Path(machine.ssh_config).parent,
                    user=machine.user,
                    port=machine.port,
                    proxy_jump=machine.proxy_jump,
                    identity_file=machine.identity_file,
                    config_file=machine.ssh_config,
                )
            else:
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

    async def idle_gpus(self, lab: str) -> list[int]:
        """All currently idle GPUs on ``lab`` (no SQLite leasing)."""
        host = self.hosts.get(lab)
        if host is None:
            return []
        thresholds = self.thresholds.get(lab, GpuThresholds())
        return sorted(
            g.index for g in await host.gpu_status() if is_idle(g, thresholds)
        )

    async def open_session(
        self,
        payload: LabReproducePayload,
        repo: str,
        number: int,
        code: dict[str, str],
        holder: str,
        binding: LabBinding | None = None,
        progress: Callable[[LabProgress], Awaitable[None]] | None = None,
        job: str | None = None,
        *,
        sync_git: Callable[[str, str | None], Awaitable[None]] | None = None,
        default_branch: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        """Open a session on the test machine: expose all currently idle GPUs (no
        leasing), create a real git worktree from a bare mirror when ``sync_git`` is
        given (else unpack archive copies), and keep the session until ``close_session``
        or the time limit."""
        machine = self.machines.get(payload.lab)
        host = self.hosts.get(payload.lab)
        why = unusable(repo, payload.lab, machine, binding, payload.requested_by)
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

        root = await self._root(payload.lab, host, machine)
        run_dir = f"{root}/{repo.replace('/', '__')}-{number}-{holder[-8:]}"
        keep = keep_path(root, repo, number)
        machine_limit = time_limit(machine, binding)
        limit = (
            min(payload.timeout_seconds, machine_limit)
            if payload.timeout_seconds is not None
            else machine_limit
        )
        session_id = new_id("lab")
        env: dict[str, str] = {RUN_TAG: session_id, KEEP_VAR: keep}
        if "head" in payload.sides:
            env[HEAD_VAR] = f"{run_dir}/head"
        if "base" in payload.sides:
            env[BASE_VAR] = f"{run_dir}/base"

        # Expose all currently idle GPUs directly without leasing.
        gpus = await self.idle_gpus(payload.lab)
        if not gpus and payload.needs_gpu:
            return {
                "lab": payload.lab,
                "deferred": True,
                "reason": "no idle GPU on the machine; nothing ran",
            }
        env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, gpus))
        if gpus:
            await tell(phase=f"Idle GPUs on {payload.lab}: {', '.join(map(str, gpus))}")

        now = self.clock.now()
        session = LabSession(
            id=session_id,
            token=secrets.token_urlsafe(32),
            lab=payload.lab,
            repo=repo,
            number=number,
            holder=holder,
            run_dir=run_dir,
            sides=list(payload.sides),
            gpus=gpus,
            env=env,
            before=setup_lines(machine, binding),
            forbid=forbidden(machine, binding),
            deadline=now + timedelta(seconds=limit),
            idle_limit=limit,
            hard_deadline=now + timedelta(seconds=max(limit, MAX_SESSION_SECONDS)),
            progress=progress,
            job=job,
        )
        await self._remember(session)

        used_git = False
        if (
            sync_git is not None
            and default_branch
            and head_sha
            and callable(getattr(host, "mirror_url", None))
            and callable(getattr(host, "ssh_command", None))
        ):
            mirror = f"{root}/mirrors/{repo.replace('/', '__')}.git"
            await tell(phase=f"Syncing git mirror on {payload.lab}")
            init = await host.run(
                f"mkdir -p {shlex.quote(root + '/mirrors')} {shlex.quote(keep)} "
                f"{shlex.quote(run_dir)} && touch {shlex.quote(keep + '/' + KEEP_MARK)} "
                f"&& ([ -d {shlex.quote(mirror + '/objects')} ] || git init -q --bare {shlex.quote(mirror)})",
                timeout=120,
            )
            if init.exit_code == 0:
                await sync_git(host.mirror_url(mirror), host.ssh_command())  # type: ignore[attr-defined]
                head_wt = f"{run_dir}/head"
                await tell("head", phase=f"Checking out PR worktree on {payload.lab}")
                wt_cmd = (
                    f"git --git-dir {shlex.quote(mirror)} worktree add -q --detach "
                    f"{shlex.quote(head_wt)} {shlex.quote(head_sha)}"
                )
                wt_res = await host.run(wt_cmd, timeout=300)
                if wt_res.exit_code == 0:
                    # Restore instruction/skill files from default branch so PR cannot hijack them.
                    await host.run(
                        _RESTORE_INSTRUCTIONS_SH
                        % {
                            "wt": shlex.quote(head_wt),
                            "ref": shlex.quote(f"origin/{default_branch}"),
                        },
                        timeout=120,
                    )
                    if "base" in payload.sides and base_sha:
                        await host.run(
                            f"git --git-dir {shlex.quote(mirror)} worktree add -q --detach "
                            f"{shlex.quote(run_dir + '/base')} {shlex.quote(base_sha)}",
                            timeout=300,
                        )
                    session.worktree = head_wt
                    session.mirror = mirror
                    used_git = True

        if not used_git:
            # Archive fallback (fakes, non-git machines, or when git mirror sync is unavailable).
            lasting = f"mkdir -p {shlex.quote(keep)} && touch {shlex.quote(keep + '/' + KEEP_MARK)} && "
            for side in payload.sides:
                if side not in code:
                    continue
                await tell(side, phase=f"Copying the {side} code to {payload.lab}")
                put = await host.run(
                    lasting
                    + f"mkdir -p {shlex.quote(run_dir + '/' + side)} && base64 -d "
                    f"| tar -xzf - -C {shlex.quote(run_dir + '/' + side)}",
                    timeout=900,
                    stdin=code[side],
                )
                lasting = ""
                if put.exit_code != 0:
                    await self.close_session(session.id)
                    return {
                        "lab": payload.lab,
                        "error": f"could not copy the {side} code to the machine",
                        "tail": _tail(put.stdout + "\n" + put.stderr),
                    }

        self._sessions[session.id] = session
        self._opening.pop(session.id, None)
        session.ready = True
        await self._save(session)
        await tell(
            phase=f"The agent works on {payload.lab} for at most "
            f"{round(limit / 60)} minutes"
        )
        return {
            "lab": payload.lab,
            "session": session.id,
            "gpu": session.gpu,
            "gpus": session.gpus,
            "sides": session.sides,
            "worktree": session.worktree,
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
            "gpus": session.gpus,
            "seconds_left": max(0, int(left)),
            "run_dir": session.run_dir,
            "keep_dir": session.env.get(KEEP_VAR),
        }

    def session_owner(self, token: str) -> dict[str, Any] | None:
        """Who holds the session of ``token``, for the service rather than the agent:
        the job whose agent works in it, its repository and PR, and the machine."""
        session = self._by_token(token)
        if session is None:
            return None
        return {
            "job": session.job,
            "repo": session.repo,
            "number": session.number,
            "lab": session.lab,
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
        now = self.clock.now()
        hard_deadline = session.hard_deadline or session.deadline
        hard_left = (hard_deadline - now).total_seconds()
        if hard_left <= 0 or (session.active_commands == 0 and session.deadline <= now):
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
        # Slide the inactivity deadline forward while commands run so large
        # checkpoint/dataset downloads or long GPU runs are never cut off early;
        # callers may still bound a single command with ``timeout_seconds``.
        session.slide(now)
        timeout = min(hard_left, timeout_seconds) if timeout_seconds else hard_left
        merged: deque[str] = deque(maxlen=TAIL_LINES)
        last_saved = now

        async def output(line: str) -> None:
            nonlocal last_saved
            merged.append(line)
            cur = self.clock.now()
            session.slide(cur)
            if (cur - last_saved).total_seconds() >= SLIDE_SAVE_INTERVAL:
                last_saved = cur
                with contextlib.suppress(Exception):
                    await self._save(session)
            await session.tell(side, line=line)
            if on_line is not None:
                with contextlib.suppress(Exception):
                    await on_line(line)

        session.active_commands += 1
        try:
            async with session.lock:
                await session.tell(side, phase=f"$ {command[:300]}")
                result = await host.run(
                    "\n".join([*session.before, command]),
                    cwd=f"{session.run_dir}/{side}",
                    env=session.env,
                    timeout=timeout,
                    on_output=output,
                )
        finally:
            session.active_commands = max(0, session.active_commands - 1)
            session.slide(self.clock.now())
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
        await self._save(session)
        return outcome

    async def grow_gpus(
        self, token: str, count: int, wait_seconds: int = 0
    ) -> dict[str, Any]:
        """Refresh the session's visible idle GPUs (no SQLite leasing): wait up to
        ``wait_seconds`` until at least ``count`` GPUs are idle, or set to empty when
        ``count == 0``."""
        session = self._by_token(token)
        if session is None:
            return {"error": "there is no open session for this token"}
        host = self.hosts.get(session.lab)
        if host is None:
            return {"error": f"there is no test machine named {session.lab!r}"}
        count = max(0, min(int(count), 64))
        if count == 0:
            session.gpus = []
        else:
            until = min(
                self.clock.now() + timedelta(seconds=max(0, wait_seconds)),
                session.deadline,
            )
            waited = False
            while True:
                idle = await self.idle_gpus(session.lab)
                if len(idle) >= count or self.clock.now() >= until:
                    session.gpus = idle
                    break
                if not waited:
                    waited = True
                    await session.tell(
                        phase=f"Waiting for {count} idle GPUs ({len(idle)} idle now)"
                    )
                await asyncio.sleep(GPU_POLL_SECONDS)
        session.env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, session.gpus))
        await self._save(session)
        await session.tell(
            phase=f"Visible GPUs: {', '.join(map(str, session.gpus))}"
            if session.gpus
            else "No GPUs selected"
        )
        return {"gpus": session.gpus, "wanted": count}

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
        await self._save(session)
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
            await self._clean(
                session.lab,
                session.run_dir,
                session.id,
                mirror=session.mirror,
            )
            await self._forget(session_id)
        return {
            "lab": session.lab,
            "gpu": session.gpu,
            "gpus": session.gpus,
            "commands": session.log,
        }

    async def expire_sessions(self) -> int:
        """Close sessions that are idle past their sliding deadline or past their
        hard wall-clock ceiling; never interrupt an actively running command before
        its hard ceiling."""
        now = self.clock.now()
        late = [
            s.id
            for s in self._sessions.values()
            if (s.hard_deadline or s.deadline) <= now
            or (s.active_commands == 0 and s.deadline <= now)
        ]
        for session_id in late:
            await self.close_session(session_id)
        return len(late)

    async def recover_sessions(self) -> int:
        """After a restart: keep the sessions the last process left open that are
        ready and have time left, with new tokens, for their jobs' agents to resume
        in (the folders, environments, and GPU stay theirs); clean up the rest.
        Returns how many were cleaned up."""
        left = await self.store.get_state(SESSIONS_KEY) or {}
        now = self.clock.now()
        kept: dict[str, Any] = {}
        cleaned = 0
        for session_id, record in left.items():
            session = self._adopted(session_id, record, now)
            if session is not None:
                self._sessions[session.id] = session
                kept[session.id] = session.record()
                continue
            cleaned += 1
            with contextlib.suppress(Exception):
                await self._clean(
                    record["lab"],
                    record["run_dir"],
                    str(record.get("id") or session_id),
                    mirror=record.get("mirror"),
                )
        await self.store.set_state(SESSIONS_KEY, kept)
        return cleaned

    def _adopted(
        self, session_id: str, record: dict[str, Any], now: datetime
    ) -> LabSession | None:
        """The session ``record`` describes, under a new token, when it can go on:
        its code was on the machine and it has a minute or more left."""
        try:
            deadline = datetime.fromisoformat(str(record["deadline"]))
            if not record.get("ready") or deadline <= now + timedelta(minutes=1):
                return None
            session = LabSession(
                id=str(record.get("id") or session_id),
                token=secrets.token_urlsafe(32),
                lab=str(record["lab"]),
                repo=str(record["repo"]),
                number=int(record["number"]),
                holder=str(record["holder"]),
                run_dir=str(record["run_dir"]),
                sides=list(record["sides"]),
                gpus=_gpus(record),
                env=dict(record.get("env") or {}),
                before=list(record.get("before") or []),
                forbid=list(record.get("forbid") or []),
                deadline=deadline,
                idle_limit=int(record.get("idle_limit") or 1800),
                hard_deadline=datetime.fromisoformat(str(record["hard_deadline"]))
                if record.get("hard_deadline")
                else deadline,
                job=record.get("job"),
                ready=True,
                worktree=record.get("worktree"),
                mirror=record.get("mirror"),
                log=list(record.get("log") or []),
            )
        except (KeyError, TypeError, ValueError):
            return None
        sink, job = self.progress_sink, session.job
        if sink is not None and job:
            repo, number, lab = session.repo, session.number, session.lab

            async def progress(step: LabProgress) -> None:
                await sink(job, repo, number, lab, step)

            session.progress = progress
        return session

    def _by_token(self, token: str) -> LabSession | None:
        for session in self._sessions.values():
            if not session.closed and secrets.compare_digest(session.token, token):
                return session
        return None

    async def _clean(
        self,
        lab: str,
        run_dir: str,
        session_id: str | None = None,
        *,
        mirror: str | None = None,
    ) -> None:
        """Stop what the session started (``STOP``), remove any git worktrees and
        its run folder. The PR's lasting folder stays."""
        host = self.hosts.get(lab)
        if host is not None:
            wt_clean = ""
            if mirror:
                qmirror = shlex.quote(mirror)
                wt_clean = (
                    f"git --git-dir {qmirror} worktree remove --force {shlex.quote(run_dir + '/head')} 2>/dev/null || true; "
                    f"git --git-dir {qmirror} worktree remove --force {shlex.quote(run_dir + '/base')} 2>/dev/null || true; "
                    f"git --git-dir {qmirror} worktree prune 2>/dev/null || true; "
                )
            with contextlib.suppress(Exception):
                await host.run(
                    STOP
                    % {
                        "tag": shlex.quote(
                            f"{RUN_TAG}={session_id}" if session_id else ""
                        ),
                        "dir": shlex.quote(run_dir),
                    }
                    + wt_clean
                    + f"rm -rf {shlex.quote(run_dir)}",
                    timeout=300,
                )

    async def _root(self, lab: str, host: LabHost, machine: LabMachine) -> str:
        """The machine's work folder, made absolute against its login's home."""
        root = machine.workdir.rstrip("/")
        if root.startswith("/"):
            return root
        home = await host.run('printf %s "$HOME"', timeout=60)
        if home.exit_code != 0 or not home.stdout.strip():
            raise RuntimeError(f"cannot find the home folder on {lab}")
        return f"{home.stdout.strip()}/{root}"

    async def pr_open(self, repo: str, number: int) -> bool:
        """Whether the service knows ``repo#number`` as an open PR."""
        snapshot = await self.store.get_snapshot(repo, number)
        return snapshot is not None and snapshot.state == PrState.OPEN

    async def sweep(
        self, still_open: Callable[[str, int], Awaitable[bool]] | None = None
    ) -> int:
        """Look over each machine for what sessions left behind, and remove it: run
        folders and tagged processes older than ``STRAY_HOURS`` that no open session
        claims, and PRs' lasting folders whose PR is no longer open (``still_open``)
        or that went ``KEEP_DAYS`` without a session. Returns how many it removed."""
        still_open = still_open or self.pr_open
        removed = 0
        open_sessions = [*self._sessions.values(), *self._opening.values()]
        claimed_runs = {s.run_dir for s in open_sessions}
        claimed_ids = {s.id for s in open_sessions}
        busy_prs = {(s.repo, s.number) for s in open_sessions}
        stray = STRAY_HOURS * 3600
        for lab, host in list(self.hosts.items()):
            machine = self.machines.get(lab)
            if machine is None:
                continue
            try:
                root = await self._root(lab, host, machine)
                seen = await host.run(
                    SURVEY
                    % {
                        "root": shlex.quote(root),
                        "keep": KEEP_FOLDER,
                        "mark": KEEP_MARK,
                        "tag": RUN_TAG,
                    },
                    timeout=120,
                )
            except Exception:  # a machine that does not answer is looked at later
                continue
            gone: list[str] = []
            for line in seen.stdout.splitlines():
                kind, _, rest = line.partition(" ")
                parts = rest.split()
                if kind == "R" and len(parts) == 2 and parts[1].isdigit():
                    path = f"{root}/{parts[0]}"
                    if (
                        RUN_FOLDER.match(parts[0])
                        and path not in claimed_runs
                        and int(parts[1]) > stray
                    ):
                        gone.append(
                            STOP % {"tag": "''", "dir": shlex.quote(path)}
                            + f"rm -rf {shlex.quote(path)}"
                        )
                elif kind == "K" and len(parts) == 2 and parts[1].isdigit():
                    found = PR_FOLDER.match(parts[0])
                    if found is None:
                        continue
                    repo = f"{found.group(1)}/{found.group(2)}"
                    number = int(found.group(3))
                    if (repo, number) in busy_prs:
                        continue
                    old = int(parts[1]) > KEEP_DAYS * 86400
                    if old or not await still_open(repo, number):
                        gone.append(
                            f"rm -rf {shlex.quote(keep_path(root, repo, number))}"
                        )
                elif kind == "P" and len(parts) == 3 and parts[1].isdigit():
                    pid, age, tag = parts
                    if tag not in claimed_ids and int(age) > stray and pid.isdigit():
                        gone.append(f"kill -KILL {pid} 2>/dev/null || true")
            for command in gone:
                with contextlib.suppress(Exception):
                    await host.run(command, timeout=300)
                    removed += 1
        return removed

    async def _remember(self, session: LabSession) -> None:
        self._opening[session.id] = session
        await self._save(session)

    async def _save(self, session: LabSession) -> None:
        """Record the open session, so a restart can adopt or clean it up."""
        if session.closed:
            return
        open_ = await self.store.get_state(SESSIONS_KEY) or {}
        open_[session.id] = session.record()
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
            idle = await self.idle_gpus(payload.lab)
            if not idle:
                if not template.cpu_fallback:
                    return {
                        "passed": False,
                        "deferred": True,
                        "reason": "no idle GPU on the lab; GPU tests were deferred",
                    }
                command_template, deferred = template.cpu_fallback, True
                env["CUDA_VISIBLE_DEVICES"] = ""
            else:
                gpu = idle[0]
                env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, idle))
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


def _gpus(record: dict[str, Any]) -> list[int]:
    """The GPUs a saved session holds, also from a record of one GPU."""
    if record.get("gpus") is not None:
        return [int(g) for g in record["gpus"]]
    return [] if record.get("gpu") is None else [int(record["gpu"])]


def _tail(text: str) -> list[str]:
    """The last lines of a run's output, without anything that looks like a secret."""
    text, _ = redact_secrets(text)
    return [line[:400] for line in text.strip().splitlines()[-TAIL_LINES:]]
