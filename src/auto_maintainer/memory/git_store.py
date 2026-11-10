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

"""The memory repo as a git checkout (design 5.11).

Reads come from the default branch's working tree. Each change is committed on
its own branch through a temporary worktree and merged with ``--no-ff``, so the
history shows every change and who approved it. Credentials for the remote come
from a credential helper that reads an environment variable, never the URL.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..domain.memory import MemoryChange
from ..subproc import ProcError, run

TokenFn = Callable[[], Awaitable[str | None]]

_HELPER = "!f() { echo username=$AM_GIT_USER; echo password=$AM_GIT_TOKEN; }; f"


class GitMemoryStore:
    """Implements ``MemoryStore``."""

    def __init__(
        self,
        path: Path,
        remote_url: str | None = None,
        remote_repo: str | None = None,
        branch: str = "main",
        token: TokenFn | None = None,
        git_user: str = "x-access-token",
        author_name: str = "auto-maintainer",
        author_email: str = "auto-maintainer@users.noreply.github.com",
    ) -> None:
        self.path = path
        self.remote_url = remote_url
        self._remote_repo = remote_repo
        self.branch = branch
        self._token = token
        self._git_user = git_user
        """The user name git presents with the token on the memory repo's forge."""
        self._author = (author_name, author_email)
        self._lock = asyncio.Lock()

    def remote_repo(self) -> str | None:
        return self._remote_repo

    async def _env(self) -> dict[str, str]:
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
        env.update(
            GIT_AUTHOR_NAME=self._author[0],
            GIT_AUTHOR_EMAIL=self._author[1],
            GIT_COMMITTER_NAME=self._author[0],
            GIT_COMMITTER_EMAIL=self._author[1],
            GIT_TERMINAL_PROMPT="0",
        )
        env["AM_GIT_USER"] = self._git_user
        if self._token:
            token = await self._token()
            if token:
                env["AM_GIT_TOKEN"] = token
        return env

    async def _git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> str:
        argv = [
            "git",
            "-c",
            f"credential.helper={_HELPER}",
            "-c",
            "core.quotepath=off",
            *args,
        ]
        result = await run(
            argv, cwd=cwd or self.path, env=await self._env(), timeout=300, check=check
        )
        return result.stdout.strip()

    async def open(self) -> None:
        """Clone or initialize the checkout."""
        if (self.path / ".git").exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.remote_url:
            await run(
                [
                    "git",
                    "-c",
                    f"credential.helper={_HELPER}",
                    "clone",
                    "--branch",
                    self.branch,
                    self.remote_url,
                    str(self.path),
                ],
                env=await self._env(),
                timeout=600,
                check=True,
            )
            return
        self.path.mkdir(parents=True, exist_ok=True)
        await self._git("init", "-q", "-b", self.branch)
        (self.path / "MEMORY.md").write_text(
            "# Memory\n\nIndex of what the service has learned. Keep it under 200 lines.\n",
            encoding="utf-8",
        )
        await self._git("add", "-A")
        await self._git("commit", "-q", "-m", "memory: initialize")

    # -- reads -----------------------------------------------------------------------

    async def read_text(self, path: str) -> str | None:
        target = (self.path / path).resolve()
        if not str(target).startswith(str(self.path.resolve())) or not target.is_file():
            return None
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def list_files(self, prefix: str) -> list[str]:
        out = await self._git("ls-files", "--", prefix or ".")
        return [line for line in out.splitlines() if line]

    async def head(self) -> str:
        return await self._git("rev-parse", "HEAD")

    async def refresh(self) -> bool:
        if not self.remote_url:
            return False
        async with self._lock:
            before = await self.head()
            await self._git("fetch", "-q", "origin", self.branch)
            await self._git(
                "merge", "-q", "--ff-only", f"origin/{self.branch}", check=False
            )
            return await self.head() != before

    # -- writes (the actuator only) -----------------------------------------------------

    def _write_files(self, root: Path, files: dict[str, str | None]) -> None:
        for rel, text in files.items():
            target = (root / rel).resolve()
            if not str(target).startswith(str(root.resolve()) + os.sep):
                raise ValueError(f"memory path {rel!r} escapes the repository")
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")

    async def _push(self, ref: str) -> None:
        if self.remote_url:
            await self._git("push", "-q", "origin", ref)

    async def commit_direct(
        self, files: dict[str, str | None], message: str, author: str
    ) -> str:
        async with self._lock:
            self._write_files(self.path, files)
            await self._git("add", "-A", "--", *files)
            status = await self._git("status", "--porcelain", "--", *files)
            if status:
                await self._git(
                    "commit", "-q", "-m", f"{message}\n\nRequested-by: {author}"
                )
                await self._push(self.branch)
            return await self.head()

    async def _staged_tip(self, branch: str) -> str | None:
        """Where ``branch`` already points, preferring the remote's copy, or ``None``
        when it has not been staged."""
        if self.remote_url:
            await self._git(
                "fetch",
                "-q",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                check=False,
            )
            remote_ref = f"refs/remotes/origin/{branch}"
            if await self._git("rev-parse", "-q", "--verify", remote_ref, check=False):
                return remote_ref
        local_ref = f"refs/heads/{branch}"
        if await self._git("rev-parse", "-q", "--verify", local_ref, check=False):
            return local_ref
        return None

    async def stage_change(self, change: MemoryChange) -> str:
        """Commit ``change.files`` on the change's branch and push it.

        Staging again (a lock stamped with its approvals) adds a commit on top of the
        branch as it was pushed, so the push fast-forwards and the memory PR keeps
        its history.
        """
        branch = change.branch or f"am/mem/{change.id}"
        async with self._lock:
            start = await self._staged_tip(branch) or self.branch
            tmp = Path(tempfile.mkdtemp(prefix="am-mem-"))
            try:
                await self._git(
                    "worktree", "add", "-q", "-B", branch, str(tmp / "wt"), start
                )
                wt = tmp / "wt"
                self._write_files(wt, change.files)
                await self._git("add", "-A", cwd=wt)
                body = change.description or ""
                await self._git(
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    f"{change.title}\n\n{body}\n\nMemory-Change: {change.id}\nTier: {change.tier}",
                    cwd=wt,
                )
                await self._push(branch)
            finally:
                await self._git(
                    "worktree", "remove", "--force", str(tmp / "wt"), check=False
                )
                shutil.rmtree(tmp, ignore_errors=True)
        return branch

    async def merge_change(self, change: MemoryChange) -> str:
        branch = change.branch or f"am/mem/{change.id}"
        approvals = "\n".join(
            f"Approved-by: {login} ({repo})"
            for repo, logins in change.approvals.items()
            for login in logins
        )
        async with self._lock:
            if self.remote_url:
                await self._git("fetch", "-q", "origin", self.branch)
                await self._git(
                    "merge", "-q", "--ff-only", f"origin/{self.branch}", check=False
                )
            try:
                await self._git(
                    "merge",
                    "-q",
                    "--no-ff",
                    branch,
                    "-m",
                    f"Merge memory change: {change.title}\n\n{approvals}".strip(),
                )
            except ProcError:
                await self._git("merge", "--abort", check=False)
                raise
            await self._push(self.branch)
            await self._git("branch", "-q", "-D", branch, check=False)
            return await self.head()

    async def drop_change(self, change: MemoryChange) -> None:
        if change.branch:
            async with self._lock:
                await self._git("branch", "-q", "-D", change.branch, check=False)
