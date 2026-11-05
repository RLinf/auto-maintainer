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

"""The service updates its own checkout, only to commits on its default branch.

A deploy checks the target commit out in a staging worktree, syncs its environment,
and runs its tests there. Only when they pass does the service's own checkout move to
the commit (a fast-forward of the branch, or a detached checkout), after which the
environment is synced again and the dashboard rebuilt when Node is available. The
previous commit is written to ``deploy.json`` so that ``am supervise`` can roll back
if the new version fails to start. The caller then restarts the service: ``am serve``
exits with ``RESTART_EXIT_CODE`` and the supervisor (or systemd) starts it again.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..domain.ports import CommitInfo, DeployResult, UpdateStatus
from ..subproc import run

RESTART_EXIT_CODE = 75
"""``am serve`` exits with this code to ask its supervisor for a restart."""

DEFAULT_TEST = [
    "uv",
    "run",
    "--frozen",
    "pytest",
    "-q",
    "-x",
    "-m",
    "not live_agent and not live",
]


class GitServiceUpdater:
    """Implements ``ServiceUpdater`` over the git checkout the service runs from.

    Args:
        checkout: The service's own git checkout.
        state_dir: Where staging worktrees and ``deploy.json`` go.
        branch: The default branch of the service's repository.
        remote: The git remote that points at the service's repository.
        test_command: Run in the staging worktree; a non-zero exit stops the deploy.
    """

    def __init__(
        self,
        checkout: Path,
        state_dir: Path,
        *,
        branch: str = "main",
        remote: str = "origin",
        test_command: list[str] | None = None,
        sync_command: list[str] | None = None,
    ) -> None:
        self.checkout = checkout
        self.state_dir = state_dir
        self.branch = branch
        self.remote = remote
        self.test_command = test_command or DEFAULT_TEST
        self.sync_command = sync_command or ["uv", "sync", "--frozen"]

    @property
    def state_file(self) -> Path:
        return self.state_dir / "deploy.json"

    async def _git(
        self, *args: str, cwd: Path | None = None, timeout: float = 300
    ) -> str:
        result = await run(["git", *args], cwd=cwd or self.checkout, timeout=timeout)
        if not result.ok:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()[-500:]}"
            )
        return result.stdout.strip()

    async def confirm(self) -> dict | None:
        """Mark the last deploy healthy; return its record the first time, else ``None``."""
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if state.get("reported"):
            return None
        state.update({"confirmed": True, "reported": True})
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        return state

    async def status(self) -> UpdateStatus:
        try:
            running = await self._git("rev-parse", "HEAD")
            branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
            dirty = bool(
                await self._git("status", "--porcelain", "--untracked-files=no")
            )
        except RuntimeError as e:
            return UpdateStatus(running_sha="", error=str(e))
        try:
            await self._git("fetch", "-q", self.remote, self.branch, timeout=120)
            upstream = await self._git("rev-parse", f"{self.remote}/{self.branch}")
            log = await self._git(
                "log", "--format=%H%x09%an%x09%s", f"HEAD..{self.remote}/{self.branch}"
            )
        except RuntimeError as e:
            return UpdateStatus(
                running_sha=running, branch=branch, dirty=dirty, error=str(e)
            )
        pending = []
        for line in log.splitlines():
            sha, _, rest = line.partition("\t")
            author, _, subject = rest.partition("\t")
            pending.append(CommitInfo(sha=sha, author=author, subject=subject))
        return UpdateStatus(
            running_sha=running,
            branch=branch,
            upstream_sha=upstream,
            pending=pending,
            dirty=dirty,
        )

    async def deploy(self, sha: str) -> DeployResult:
        log: list[str] = []
        previous = await self._git("rev-parse", "HEAD")
        if await self._git("status", "--porcelain", "--untracked-files=no"):
            return DeployResult(
                ok=False,
                sha=sha,
                previous_sha=previous,
                log="the checkout has local changes",
            )
        await self._git("fetch", "-q", self.remote, self.branch, timeout=120)
        target = await self._git("rev-parse", f"{sha}^{{commit}}")
        ancestor = await run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                target,
                f"{self.remote}/{self.branch}",
            ],
            cwd=self.checkout,
        )
        if not ancestor.ok:
            return DeployResult(
                ok=False,
                sha=target,
                previous_sha=previous,
                log=f"{target[:12]} is not on {self.remote}/{self.branch}",
            )
        staging = self.state_dir / "releases" / target[:12]
        shutil.rmtree(staging, ignore_errors=True)
        staging.parent.mkdir(parents=True, exist_ok=True)
        await self._git("worktree", "add", "-q", "--detach", str(staging), target)
        try:
            for argv in (self.sync_command, self.test_command):
                result = await run(argv, cwd=staging, timeout=1800)
                log.append(
                    f"$ {' '.join(argv)}\n{(result.stdout + result.stderr)[-4000:]}"
                )
                if not result.ok:
                    return DeployResult(
                        ok=False, sha=target, previous_sha=previous, log="\n".join(log)
                    )
        finally:
            await run(
                ["git", "worktree", "remove", "--force", str(staging)],
                cwd=self.checkout,
            )
            shutil.rmtree(staging, ignore_errors=True)

        branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == self.branch:
            await self._git("merge", "--ff-only", "-q", target)
        else:
            await self._git("checkout", "-q", "--detach", target)
        sync = await run(self.sync_command, cwd=self.checkout, timeout=1800)
        log.append(
            f"$ {' '.join(self.sync_command)}\n{(sync.stdout + sync.stderr)[-2000:]}"
        )
        if (self.checkout / "ui" / "package.json").is_file() and shutil.which("npm"):
            for argv in (
                ["npm", "ci", "--no-audit", "--no-fund"],
                ["npm", "run", "build"],
            ):
                built = await run(argv, cwd=self.checkout / "ui", timeout=900)
                log.append(
                    f"$ {' '.join(argv)} (ui)\n{(built.stdout + built.stderr)[-1000:]}"
                )
                if not built.ok:
                    log.append("the dashboard build failed; the previous build stays")
                    break
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "previous_sha": previous,
                    "sha": target,
                    "at": datetime.now(UTC).isoformat(),
                    "confirmed": False,
                }
            ),
            encoding="utf-8",
        )
        return DeployResult(
            ok=True, sha=target, previous_sha=previous, log="\n".join(log)
        )
