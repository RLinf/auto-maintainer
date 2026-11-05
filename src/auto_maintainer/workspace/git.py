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

"""Worktrees for agent runs from bare mirrors the service owns (design 4.7), and the
sandbox for repository check scripts."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..domain.ports import CommandResult
from ..subproc import run

TokenFn = Callable[[str], Awaitable[str | None]]

_HELPER = "!f() { echo username=x-access-token; echo password=$AM_GIT_TOKEN; }; f"

INSTRUCTION_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursorrules",
    "copilot-instructions.md",
}
INSTRUCTION_DIRS = (
    ".claude/",
    ".agents/",
    ".codex/",
    ".cursor/",
    ".gemini/",
    ".opencode/",
)


def is_instruction_path(path: str) -> bool:
    """Files that steer coding agents; they always come from the default branch."""
    name = path.rsplit("/", 1)[-1]
    return (
        name in INSTRUCTION_NAMES
        or path.startswith(INSTRUCTION_DIRS)
        or any(f"/{d}" in f"/{path}" for d in INSTRUCTION_DIRS)
    )


class GitWorkspace:
    """Implements ``Workspace``."""

    def __init__(
        self,
        root: Path,
        token: TokenFn | None = None,
        forge_url: str = "https://github.com",
    ) -> None:
        self.root = root
        self._token = token
        self._forge_url = forge_url.rstrip("/")
        self._locks: dict[str, asyncio.Lock] = {}

    async def _env(self, repo: str) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "PATH",
                "HOME",
                "LANG",
                "HTTPS_PROXY",
                "https_proxy",
                "HTTP_PROXY",
                "http_proxy",
                "NO_PROXY",
                "no_proxy",
            )
        }
        env["GIT_TERMINAL_PROMPT"] = "0"
        if self._token:
            token = await self._token(repo)
            if token:
                env["AM_GIT_TOKEN"] = token
        return env

    async def _git(
        self, repo: str, *args: str, cwd: Path, check: bool = True, timeout: float = 900
    ) -> str:
        argv = [
            "git",
            "-c",
            f"credential.helper={_HELPER}",
            "-c",
            "core.quotepath=off",
            *args,
        ]
        return (
            await run(
                argv, cwd=cwd, env=await self._env(repo), timeout=timeout, check=check
            )
        ).stdout

    def mirror(self, repo: str) -> Path:
        return self.root / "mirrors" / f"{repo.replace('/', '__')}.git"

    async def _ensure_mirror(self, repo: str) -> Path:
        path = self.mirror(repo)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            await run(
                [
                    "git",
                    "-c",
                    f"credential.helper={_HELPER}",
                    "clone",
                    "--bare",
                    "--filter=blob:none",
                    f"{self._forge_url}/{repo}.git",
                    str(path),
                ],
                env=await self._env(repo),
                timeout=1800,
                check=True,
            )
        return path

    async def prepare(
        self, repo: str, number: int | None, sha: str, default_branch: str
    ) -> Path:
        lock = self._locks.setdefault(repo, asyncio.Lock())
        async with lock:
            mirror = await self._ensure_mirror(repo)
            refspecs = [
                f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}"
            ]
            if number is not None:
                refspecs.append(f"+refs/pull/{number}/head:refs/pull/{number}/head")
            await self._git(repo, "fetch", "-q", "origin", *refspecs, cwd=mirror)
            have = await run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=mirror)
            if not have.ok:
                await self._git(repo, "fetch", "-q", "origin", sha, cwd=mirror)
            path = (
                self.root
                / "worktrees"
                / f"{repo.replace('/', '__')}-{sha[:12]}-{uuid.uuid4().hex[:6]}"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._git(
                repo, "worktree", "add", "-q", "--detach", str(path), sha, cwd=mirror
            )
        await self._restore_instructions(repo, path, sha, f"origin/{default_branch}")
        return path

    async def _restore_instructions(
        self, repo: str, path: Path, sha: str, default_ref: str
    ) -> None:
        head_files = set(
            (
                await self._git(repo, "ls-tree", "-r", "--name-only", sha, cwd=path)
            ).splitlines()
        )
        base_files = set(
            (
                await self._git(
                    repo,
                    "ls-tree",
                    "-r",
                    "--name-only",
                    default_ref,
                    cwd=path,
                    check=False,
                )
            ).splitlines()
        )
        restore = sorted(p for p in base_files if is_instruction_path(p))
        remove = sorted(p for p in head_files - base_files if is_instruction_path(p))
        for rel in remove:
            (path / rel).unlink(missing_ok=True)
        for i in range(0, len(restore), 200):
            await self._git(
                repo, "checkout", default_ref, "--", *restore[i : i + 200], cwd=path
            )

    async def diff(
        self, path: Any, base: str, head: str, max_bytes: int = 200_000
    ) -> str:
        repo_hint = ""
        result = await run(
            ["git", "diff", "--no-color", f"{base}...{head}"], cwd=path, timeout=300
        )
        if not result.ok and base and len(base) >= 7:
            await run(
                ["git", "fetch", "-q", "origin", base],
                cwd=path,
                env=await self._env(repo_hint),
                timeout=300,
            )
            result = await run(
                ["git", "diff", "--no-color", f"{base}...{head}"], cwd=path, timeout=300
            )
        text = result.stdout if result.ok else ""
        return text[:max_bytes] + (
            "\n[diff truncated]" if len(text) > max_bytes else ""
        )

    async def release(self, path: Any) -> None:
        p = Path(path)
        common = (
            await run(["git", "rev-parse", "--git-common-dir"], cwd=p)
            if p.exists()
            else None
        )
        if common and common.ok:
            mirror = (p / common.stdout.strip()).resolve()
            await run(["git", "worktree", "remove", "--force", str(p)], cwd=mirror)
        shutil.rmtree(p, ignore_errors=True)


class LocalSandbox:
    """Implements ``Sandbox``: a minimal environment, a private HOME, and a timeout.

    It does not isolate the network; only scripts from a repository's default branch,
    which its maintainers reviewed, run here.
    """

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> CommandResult:
        home = tempfile.mkdtemp(prefix="am-sandbox-")
        try:
            base = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": home,
                "LANG": "C.UTF-8",
            }
            base.update(env or {})
            result = await run(argv, cwd=cwd, env=base, timeout=timeout)
            return CommandResult(
                exit_code=result.exit_code,
                stdout=result.stdout[-20_000:],
                stderr=result.stderr[-20_000:],
                duration_seconds=result.duration_seconds,
                timed_out=result.timed_out,
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
