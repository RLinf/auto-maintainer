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
import base64
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..domain.forges import fs_parts, slug
from ..domain.ports import CommandResult, ConflictSides, MergeAttempt, WorkChanges
from ..subproc import run

TokenFn = Callable[[str], Awaitable[str | None]]

_HELPER = "!f() { echo username=$AM_GIT_USER; echo password=$AM_GIT_TOKEN; }; f"

SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
SIDES_MAX_FILES = 200
"""Conflicted files whose sides a merge attempt reads, at most."""
SIDES_MAX_BYTES = 1_000_000
"""A side larger than this is not read (``ConflictSides``: ``None``)."""
"""Every git command in a checkout runs without hooks or a file-system monitor: both
run commands that a configuration names."""

RemoteFn = Callable[[str], tuple[str, str]]
"""A repository's clone address and the user name git presents with its token."""

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
    """Implements ``Workspace``, and ``BranchPublisher`` for the actuator.

    Args:
        root: Data directory holding the mirrors and worktrees.
        token: Returns a forge token for a repository; git reads it through a
            credential helper from ``AM_GIT_TOKEN``, never from a remote URL.
        forge_url: Base URL of the forge, when every repository is on one.
        remote: A repository's clone address and git user name, for repositories
            on several forges; overrides ``forge_url``.
        author: Name and email that commits made by the service carry.
    """

    def __init__(
        self,
        root: Path,
        token: TokenFn | None = None,
        forge_url: str = "https://github.com",
        remote: RemoteFn | None = None,
        author: tuple[str, str] = (
            "auto-maintainer",
            "auto-maintainer@users.noreply.github.com",
        ),
    ) -> None:
        self.author = author
        self.root = root
        self._token = token
        self._forge_url = forge_url.rstrip("/")
        self._remote = remote
        self._locks: dict[str, asyncio.Lock] = {}
        self._steering: dict[Path, str] = {}
        """Each prepared checkout's default branch ref, whose instruction files it
        holds (``_restore_instructions``)."""
        self._admins: dict[Path, tuple[Path, Path]] = {}
        """Each checkout's git metadata folder and repository (``_admin``)."""

    def _admin(self, path: Path) -> tuple[Path, Path]:
        """Checkout ``path``'s git metadata folder in its mirror, and the mirror.

        An agent writes the checkout, including its ``.git`` file, and its metadata
        folder (both are mounted in its container), so neither says where git's
        configuration is: a ``.git`` or ``commondir`` naming a folder with a
        ``core.fsmonitor`` would run a command here. The mirror is the service's, and
        the metadata folder is the one in it that points back at the checkout.

        Raises:
            RuntimeError: ``path`` is not a checkout of a mirror here.
        """
        path = Path(path)
        known = self._admins.get(path)
        if known is not None:
            return known
        try:
            mirror = self.mirror(self._repo_of(path))
        except ValueError as exc:
            raise RuntimeError(f"{path} is not a checkout the service made") from exc
        own = mirror / "worktrees" / path.name
        for admin in [own, *sorted((mirror / "worktrees").glob(f"{path.name}*"))]:
            try:
                back = (admin / "gitdir").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if os.path.realpath(back) == os.path.realpath(path / ".git"):
                self._admins[path] = (admin, mirror)
                return admin, mirror
        raise RuntimeError(f"{path} is not a checkout the service made")

    def _pinned(self, path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
        """``env`` (else this process's) with git told where checkout ``path``'s
        metadata and repository are (``_admin``), not left to find them there. The
        files that name them are written back first: git still reads ``commondir``
        from the metadata folder. No agent runs in the checkout meanwhile: its
        container ends before the host works there."""
        admin, mirror = self._admin(path)
        dot = path / ".git"
        if dot.is_dir() and not dot.is_symlink():
            shutil.rmtree(dot)
        for file, text in (
            (dot, f"gitdir: {admin}\n"),
            (admin / "commondir", f"{os.path.relpath(mirror, admin)}\n"),
            (admin / "gitdir", f"{dot}\n"),
        ):
            if file.is_symlink() or file.exists():
                file.unlink()
            fd = os.open(
                file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
            )
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
        return {
            **(env if env is not None else dict(os.environ)),
            "GIT_DIR": str(admin),
            "GIT_COMMON_DIR": str(mirror),
            "GIT_WORK_TREE": str(path),
        }

    async def _in(
        self, path: Path, *args: str, repo: str | None = None, **kw: Any
    ) -> Any:
        """Run ``git args`` in checkout ``path`` (``_pinned``, ``SAFE``); with
        ``repo``, signed in to its forge for a fetch or a push."""
        argv = ["git", *SAFE]
        env = None
        if repo is not None:
            argv += ["-c", f"credential.helper={_HELPER}", "-c", "core.quotepath=off"]
            env = await self._env(repo)
        return await run(
            [*argv, *args], cwd=path, env=self._pinned(Path(path), env), **kw
        )

    def remote_url(self, repo: str) -> str:
        """Where ``repo`` is cloned from and pushed to."""
        if self._remote is not None:
            return self._remote(repo)[0]
        return f"{self._forge_url}/{repo}.git"

    def _git_user(self, repo: str) -> str:
        return self._remote(repo)[1] if self._remote is not None else "x-access-token"

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
        env["AM_GIT_USER"] = self._git_user(repo)
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
        return self.root / "mirrors" / f"{slug(repo)}.git"

    def repo_dir(self, repo: str) -> Path:
        """The repository's own folder: every checkout an agent works in for it."""
        return self.root.joinpath("repos", *fs_parts(repo))

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
                    self.remote_url(repo),
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
            what = f"pr-{number}" if number is not None else default_branch
            path = self.repo_dir(repo) / f"{what}-{sha[:12]}-{uuid.uuid4().hex[:6]}"
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._git(
                repo, "worktree", "add", "-q", "--detach", str(path), sha, cwd=mirror
            )
        await self._restore_instructions(repo, path, sha, f"origin/{default_branch}")
        self._steering[path] = f"origin/{default_branch}"
        return path

    async def _restore_instructions(
        self, repo: str, path: Path, sha: str, default_ref: str
    ) -> None:
        head_files = set(
            (
                await self._in(path, "ls-tree", "-r", "--name-only", sha, check=True)
            ).stdout.splitlines()
        )
        base_files = set(
            (
                await self._in(path, "ls-tree", "-r", "--name-only", default_ref)
            ).stdout.splitlines()
        )
        restore = sorted(p for p in base_files if is_instruction_path(p))
        remove = sorted(p for p in head_files - base_files if is_instruction_path(p))
        for rel in remove:
            (path / rel).unlink(missing_ok=True)
        for i in range(0, len(restore), 200):
            await self._in(
                path, "checkout", default_ref, "--", *restore[i : i + 200], check=True
            )

    async def diff(
        self, path: Any, base: str, head: str, max_bytes: int = 200_000
    ) -> str:
        path = Path(path)
        result = await self._in(
            path, "diff", "--no-color", f"{base}...{head}", timeout=300
        )
        if not result.ok and base and len(base) >= 7:
            await self._in(
                path,
                "fetch",
                "-q",
                "origin",
                base,
                repo=self._repo_of(path),
                timeout=300,
            )
            result = await self._in(
                path, "diff", "--no-color", f"{base}...{head}", timeout=300
            )
        text = result.stdout if result.ok else ""
        return text[:max_bytes] + (
            "\n[diff truncated]" if len(text) > max_bytes else ""
        )

    async def release(self, path: Any) -> None:
        p = Path(path)
        self._steering.pop(p, None)
        try:
            _, mirror = self._admin(p)
        except RuntimeError:
            mirror = None
        self._admins.pop(p, None)
        if mirror is not None:
            await run(
                ["git", *SAFE, "worktree", "remove", "--force", str(p)], cwd=mirror
            )
        shutil.rmtree(p, ignore_errors=True)
        if mirror is not None:
            await run(["git", "worktree", "prune"], cwd=mirror)

    async def changes(self, path: Any) -> WorkChanges:
        """What changed in the checkout since its commit: the agent's work. The
        instruction files the checkout took from the default branch (``prepare``)
        are left out, so a fix never carries them onto a PR, nor drops the PR's own;
        in a checkout of the default branch itself they are the agent's to change."""
        p = Path(path)
        await self._in(p, "add", "-A", timeout=300, check=True)
        steering = self._steering.get(p)
        if steering is not None:
            differ = await self._in(
                p,
                "diff",
                "--name-only",
                "-z",
                "HEAD",
                steering,
                "--",
                timeout=300,
                check=True,
            )
            swapped = [f for f in differ.stdout.split("\0") if is_instruction_path(f)]
            for i in range(0, len(swapped), 200):
                await self._in(
                    p, "reset", "-q", "HEAD", "--", *swapped[i : i + 200], timeout=300
                )
        base = (await self._in(p, "rev-parse", "HEAD", check=True)).stdout.strip()
        patch = (
            await self._in(
                p, "diff", "--cached", "--binary", "--no-color", timeout=300, check=True
            )
        ).stdout
        names = (
            await self._in(p, "diff", "--cached", "--name-only", check=True)
        ).stdout
        return WorkChanges(base_sha=base, patch=patch, files=names.split())

    async def apply(self, path: Any, patch: str) -> None:
        if patch.strip():
            await self._in(
                Path(path),
                "apply",
                "--binary",
                "--index",
                "--whitespace=nowarn",
                "-",
                stdin=patch,
                timeout=300,
                check=True,
            )

    def _repo_of(self, path: Path) -> str:
        """The repository a worktree under ``repo_dir`` belongs to."""
        owner, name = (
            Path(path).resolve().relative_to((self.root / "repos").resolve()).parts[:2]
        )
        return f"{owner}/{name}"

    async def merge(self, path: Any, branch: str) -> MergeAttempt:
        path = Path(path)
        repo = self._repo_of(path)
        ref = f"refs/remotes/origin/{branch}"
        await self._in(
            path,
            "fetch",
            "-q",
            "origin",
            f"+refs/heads/{branch}:{ref}",
            repo=repo,
            timeout=900,
            check=True,
        )
        merge_sha = (await self._in(path, "rev-parse", ref, check=True)).stdout.strip()
        steering_ref = self._steering.get(path)
        # The checkout holds the default branch's instruction files, which git
        # would refuse to merge over: merge the PR's own commit as it is.
        await self._in(path, "reset", "-q", "--hard", "HEAD", check=True)
        conflicts = await self._merge(path, merge_sha)
        sides = {c: await self._sides(path, c) for c in conflicts[:SIDES_MAX_FILES]}
        steering = [c for c in conflicts if is_instruction_path(c)]
        if steering_ref is not None:
            # The agent that resolves the rest works from the default branch's
            # instructions, as every agent does; a conflicted instruction file is
            # not its to resolve (``MergeAttempt.steering``).
            await self._restore_instructions(repo, path, "HEAD", steering_ref)
        return MergeAttempt(
            merge_sha=merge_sha, conflicts=conflicts, sides=sides, steering=steering
        )

    async def _sides(self, path: Path, name: str) -> ConflictSides:
        """``name`` as git's index holds it during a conflicted merge: stage 1 the
        common ancestor's, 2 the worktree's commit (the PR), 3 the merged branch's."""

        async def stage(n: int) -> str | None:
            shown = await self._in(path, "show", f":{n}:{name}", timeout=120)
            text = shown.stdout if shown.ok else None
            if text is None or len(text) > SIDES_MAX_BYTES or "\0" in text:
                return None
            return text

        return ConflictSides(
            base=await stage(1), ours=await stage(2), theirs=await stage(3)
        )

    async def _merge(self, path: Path, sha: str) -> list[str]:
        """Merge ``sha`` into the worktree without committing; the conflicted files.

        Raises:
            RuntimeError: git could not merge for a reason other than conflicts
                (an empty list would read as a clean merge).
        """
        name, email = self.author
        merged = await self._in(
            path,
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "merge",
            "--no-ff",
            "--no-commit",
            sha,
            timeout=600,
        )
        listed = await self._in(
            path, "diff", "--name-only", "-z", "--diff-filter=U", check=True
        )
        conflicts = [p for p in listed.stdout.split("\0") if p]
        if not merged.ok and not conflicts:
            tail = " ".join((merged.stderr or merged.stdout).strip().splitlines()[-3:])
            raise RuntimeError(f"git could not merge {sha[:12]}: {tail}")
        return conflicts

    async def merge_base(self, path: Any, a: str, b: str) -> str | None:
        result = await self._in(Path(path), "merge-base", a, b, timeout=120)
        if not result.ok:
            return None
        return result.stdout.strip() or None

    async def archive(self, path: Any, ref: str, max_bytes: int = 200_000_000) -> str:
        with tempfile.TemporaryDirectory(prefix="am-archive-") as tmp:
            out = Path(tmp) / "code.tar.gz"
            await self._in(
                Path(path),
                "archive",
                "--format=tar.gz",
                "-o",
                str(out),
                ref,
                timeout=600,
                check=True,
            )
            size = out.stat().st_size
            if size > max_bytes:
                raise ValueError(
                    f"the code at {ref[:12]} is {size // 1_000_000} MB packed; "
                    f"the limit is {max_bytes // 1_000_000} MB"
                )
            data = await asyncio.to_thread(out.read_bytes)
        return base64.b64encode(data).decode("ascii")

    async def publish(
        self,
        repo: str,
        base_sha: str,
        patch: str,
        branch: str,
        message: str,
        author: tuple[str, str] | None = None,
        *,
        merge_sha: str | None = None,
        resolved: dict[str, str] | None = None,
        push_to: str | None = None,
    ) -> str:
        """Apply ``patch`` on ``base_sha`` in a fresh worktree (or merge ``merge_sha``
        into it, taking ``resolved`` for the files that conflict), commit (as
        ``author``, else the service), and push ``branch`` of ``push_to`` (else
        ``repo``) without forcing, so a branch that moved meanwhile refuses it."""
        lock = self._locks.setdefault(repo, asyncio.Lock())
        async with lock:
            mirror = await self._ensure_mirror(repo)
            have = await run(
                ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=mirror
            )
            if not have.ok:
                await self._git(repo, "fetch", "-q", "origin", base_sha, cwd=mirror)
            path = self.repo_dir(repo) / f"publish-{uuid.uuid4().hex[:8]}"
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._git(
                repo,
                "worktree",
                "add",
                "-q",
                "--detach",
                str(path),
                base_sha,
                cwd=mirror,
            )
        try:
            if merge_sha:
                have = await self._in(path, "cat-file", "-e", f"{merge_sha}^{{commit}}")
                if not have.ok:
                    await self._in(
                        path,
                        "fetch",
                        "-q",
                        "origin",
                        merge_sha,
                        repo=repo,
                        timeout=900,
                        check=True,
                    )
                conflicts = await self._merge(path, merge_sha)
                missing = [c for c in conflicts if c not in (resolved or {})]
                if missing:
                    raise RuntimeError(
                        "the merge now conflicts in "
                        + ", ".join(missing)
                        + ", which the resolution does not cover; ask for it again"
                    )
                for conflict in conflicts:
                    (path / conflict).write_text(
                        (resolved or {})[conflict], encoding="utf-8"
                    )
                    await self._in(path, "add", "--", conflict, check=True)
            elif patch:
                await self._in(
                    path,
                    "apply",
                    "--index",
                    "--binary",
                    "--whitespace=nowarn",
                    "-",
                    stdin=patch,
                    timeout=300,
                    check=True,
                )
            name, email = author or self.author
            await self._in(
                path,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-q",
                "-F",
                "-",
                stdin=message,
                check=True,
            )
            sha = (await self._in(path, "rev-parse", "HEAD", check=True)).stdout.strip()
            remote = (
                self.remote_url(push_to) if push_to and push_to != repo else "origin"
            )
            await self._in(
                path,
                "push",
                "-q",
                remote,
                f"HEAD:refs/heads/{branch}",
                repo=repo,
                timeout=900,
                check=True,
            )
            return sha
        finally:
            await self.release(path)


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
        untrusted: bool = False,
    ) -> CommandResult:
        """Run ``argv`` here; ``untrusted`` code runs here too only where agents also
        run on the host (``agents.container.ContainedSandbox`` runs it elsewhere)."""
        home = tempfile.mkdtemp(prefix="am-sandbox-")
        try:
            base = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": home,
                "LANG": "C.UTF-8",
                "UV_CACHE_DIR": os.environ.get(
                    "UV_CACHE_DIR", str(Path.home() / ".cache" / "uv")
                ),
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
