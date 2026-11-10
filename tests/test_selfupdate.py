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

"""Updating the service's checkout, and the supervisor's restarts and rollbacks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from auto_maintainer.selfupdate import RESTART_EXIT_CODE, GitServiceUpdater
from auto_maintainer.selfupdate.supervise import rollback, supervise

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin, checkout, other = (
        tmp_path / "origin.git",
        tmp_path / "checkout",
        tmp_path / "other",
    )
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    (checkout / "app.txt").write_text("v1\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-q", "-m", "v1")
    git(checkout, "push", "-q", "origin", "HEAD:main")
    git(tmp_path, "clone", "-q", str(origin), str(other))
    return origin, checkout, other


def _push_change(other: Path, text: str) -> str:
    (other / "app.txt").write_text(text)
    git(other, "commit", "-q", "-am", text.strip())
    git(other, "push", "-q", "origin", "HEAD:main")
    return git(other, "rev-parse", "HEAD")


def _updater(
    checkout: Path, tmp_path: Path, test: list[str] | None = None
) -> GitServiceUpdater:
    return GitServiceUpdater(
        checkout,
        tmp_path / "state",
        test_command=test or ["true"],
        sync_command=["true"],
    )


async def test_status_and_deploy(repos, tmp_path: Path) -> None:
    _, checkout, other = repos
    before = git(checkout, "rev-parse", "HEAD")
    target = _push_change(other, "v2\n")
    updater = _updater(checkout, tmp_path)
    status = await updater.status()
    assert status.running_sha == before and status.upstream_sha == target
    assert [c.subject for c in status.pending] == ["v2"] and not status.dirty
    result = await updater.deploy(target)
    assert result.ok, result.log
    assert git(checkout, "rev-parse", "HEAD") == target
    assert (checkout / "app.txt").read_text() == "v2\n"
    state = json.loads(updater.state_file.read_text())
    assert (
        state["previous_sha"] == before
        and state["sha"] == target
        and not state["confirmed"]
    )
    record = await updater.confirm()
    assert record is not None and record["sha"] == target
    assert await updater.confirm() is None
    assert not (tmp_path / "state" / "releases" / target[:12]).exists()


async def test_failing_tests_keep_the_running_code(repos, tmp_path: Path) -> None:
    _, checkout, other = repos
    before = git(checkout, "rev-parse", "HEAD")
    target = _push_change(other, "v2\n")
    result = await _updater(checkout, tmp_path, test=["false"]).deploy(target)
    assert not result.ok and git(checkout, "rev-parse", "HEAD") == before


async def test_only_commits_on_the_default_branch_deploy(repos, tmp_path: Path) -> None:
    _, checkout, other = repos
    git(other, "checkout", "-q", "-b", "side")
    (other / "app.txt").write_text("side\n")
    git(other, "commit", "-q", "-am", "side")
    git(other, "push", "-q", "origin", "side")
    side = git(other, "rev-parse", "HEAD")
    git(checkout, "fetch", "-q", "origin", "side")
    result = await _updater(checkout, tmp_path).deploy(side)
    assert not result.ok and "is not on main of origin" in result.log


async def test_local_changes_block_a_deploy(repos, tmp_path: Path) -> None:
    _, checkout, other = repos
    target = _push_change(other, "v2\n")
    (checkout / "app.txt").write_text("edited here\n")
    result = await _updater(checkout, tmp_path).deploy(target)
    assert not result.ok and "local changes" in result.log
    assert (await _updater(checkout, tmp_path).status()).dirty


async def test_rollback_returns_to_the_previous_commit(repos, tmp_path: Path) -> None:
    _, checkout, other = repos
    before = git(checkout, "rev-parse", "HEAD")
    target = _push_change(other, "v2\n")
    updater = _updater(checkout, tmp_path)
    assert (await updater.deploy(target)).ok
    assert rollback(checkout, updater.state_file, lambda m: None, sync_command=["true"])
    assert git(checkout, "rev-parse", "HEAD") == before
    state = json.loads(updater.state_file.read_text())
    assert state["rolled_back"] and state["confirmed"]
    assert not rollback(
        checkout, updater.state_file, lambda m: None, sync_command=["true"]
    )
    reported = await updater.confirm()
    assert reported is not None and reported["rolled_back"]


def test_the_supervisor_restarts_after_a_deploy(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    script = (
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(marker)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        f"sys.exit({RESTART_EXIT_CODE} if n == 0 else 0)\n"
    )
    code = supervise(
        [sys.executable, "-c", script],
        tmp_path,
        tmp_path / "deploy.json",
        log=lambda m: None,
    )
    assert code == 0 and marker.read_text() == "2"


async def test_a_change_is_published_on_its_own_branch(tmp_path: Path) -> None:
    from auto_maintainer.workspace.git import GitWorkspace

    forge = tmp_path / "forge"
    bare = forge / "acme" / "am.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "AGENTS.md").write_text("rules\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")
    base = git(seed, "rev-parse", "HEAD")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/am", None, base, "main")
    (path / "src").mkdir()
    (path / "src" / "ping.py").write_text("print('pong')\n")
    (path / "AGENTS.md").write_text("rules\nmore rules\n")
    changes = await ws.changes(path)
    assert changes.base_sha == base and sorted(changes.files) == [
        "AGENTS.md",
        "src/ping.py",
    ]
    await ws.release(path)

    sha = await ws.publish(
        "acme/am", base, changes.patch, "am/self/ping-1", "feat: ping\n"
    )
    assert git(bare, "rev-parse", "refs/heads/am/self/ping-1") == sha
    assert git(bare, "rev-parse", "refs/heads/main") == base
    assert (
        git(bare, "log", "-1", "--format=%an %s", sha) == "auto-maintainer feat: ping"
    )
    assert git(bare, "show", f"{sha}:src/ping.py") == "print('pong')"


async def test_updates_come_from_the_service_repo_not_the_checkouts_origin(
    repos, tmp_path: Path
) -> None:
    """A host cloned from upstream runs what is merged in its owner's fork."""
    origin, checkout, _ = repos
    fork = tmp_path / "fork.git"
    git(tmp_path, "clone", "-q", "--bare", str(origin), str(fork))
    work = tmp_path / "fork-work"
    git(tmp_path, "clone", "-q", str(fork), str(work))
    target = _push_change(work, "from the fork\n")

    async def source() -> tuple[str, str, str | None]:
        return str(fork), "x-access-token", None

    updater = GitServiceUpdater(
        checkout,
        tmp_path / "state",
        source=source,
        repo="alice/auto-maintainer",
        test_command=["true"],
        sync_command=["true"],
    )
    status = await updater.status()
    assert status.upstream_sha == target and git(origin, "rev-parse", "main") != target
    assert (await updater.deploy(target)).ok
    assert (checkout / "app.txt").read_text() == "from the fork\n"


async def test_agents_read_an_export_of_the_running_code(repos, tmp_path: Path) -> None:
    """The code agents may read is the running commit's tracked files, read-only,
    without the checkout's git settings or anything untracked; one per commit."""
    from auto_maintainer.selfupdate import export_source

    _, checkout, other = repos
    (checkout / "secret.local").write_text("not tracked")
    out = await export_source(checkout, tmp_path / "service-code")
    assert out is not None and (out / "app.txt").is_file()
    assert not (out / ".git").exists() and not (out / "secret.local").exists()
    assert not (out / "app.txt").stat().st_mode & 0o222, "read-only"
    assert await export_source(checkout, tmp_path / "service-code") == out
    _push_change(other, "v2\n")
    git(checkout, "pull", "-q", "origin", "main")
    newer = await export_source(checkout, tmp_path / "service-code")
    assert newer != out and not out.exists(), "an older export is removed"
    assert await export_source(tmp_path, tmp_path / "x") is None
