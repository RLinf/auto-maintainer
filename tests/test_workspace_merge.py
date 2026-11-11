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

"""Merging a PR's base branch with real git: the conflicts, the resolution, and the
merge commit pushed onto the PR's branch in the contributor's fork."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from auto_maintainer.workspace.git import GitWorkspace

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(work: Path, text: str, message: str) -> str:
    (work / "loader.py").write_text(text)
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


async def test_a_resolved_merge_lands_on_the_forks_branch(tmp_path: Path) -> None:
    forge = tmp_path / "forge"
    upstream = forge / "acme" / "widget.git"
    fork = forge / "alice" / "widget.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(upstream), str(work))
    _commit(work, "one\n", "seed")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(fork))
    git(work, "checkout", "-q", "-b", "feature")
    head = _commit(work, "one, from the PR\n", "pr change")
    git(work, "push", "-q", str(fork), "feature")
    # GitHub keeps a PR's commits in the base repository too.
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    git(work, "checkout", "-q", "main")
    main = _commit(work, "one, on main\n", "main change")
    git(work, "push", "-q", "origin", "main")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", 7, head, "main")
    attempt = await ws.merge(path, "main")
    assert attempt.merge_sha == main and attempt.conflicts == ["loader.py"]
    assert "<<<<<<<" in (path / "loader.py").read_text()
    await ws.release(path)

    with pytest.raises(RuntimeError, match="does not cover"):
        await ws.publish("acme/widget", head, "", "feature", "merge\n", merge_sha=main)
    sha = await ws.publish(
        "acme/widget",
        head,
        "",
        "feature",
        "Merge main into #7\n",
        ("maria", "maria@x"),
        merge_sha=main,
        resolved={"loader.py": "one, from the PR and main\n"},
        push_to="alice/widget",
    )
    assert git(fork, "rev-parse", "refs/heads/feature") == sha
    assert git(fork, "log", "-1", "--format=%P", sha).split() == [head, main]
    assert git(fork, "show", f"{sha}:loader.py") == "one, from the PR and main"
    assert git(fork, "log", "-1", "--format=%an", sha) == "maria"


def _repo_with_pr(tmp_path: Path) -> tuple[Path, Path, str]:
    """An upstream whose PR #7 branched from a seed with ``AGENTS.md``; returns the
    forge, a work clone on ``main``, and the PR's head."""
    forge = tmp_path / "forge"
    upstream = forge / "acme" / "widget.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(upstream), str(work))
    (work / "AGENTS.md").write_text("seed rules\n")
    _commit(work, "one\n", "seed")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(work, "checkout", "-q", "-b", "feature")
    return forge, work, ""


async def test_a_merge_finds_the_conflicts_when_main_changed_agent_instructions(
    tmp_path: Path,
) -> None:
    """The checkout holds main's instruction files, which git would refuse to merge
    over; the merge still runs, finds the PR's conflicts, and leaves main's
    instructions for the agent."""
    forge, work, _ = _repo_with_pr(tmp_path)
    head = _commit(work, "one, from the PR\n", "pr change")
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    git(work, "checkout", "-q", "main")
    (work / "AGENTS.md").write_text("main's newer rules\n")
    main = _commit(work, "one, on main\n", "main change")
    git(work, "push", "-q", "origin", "main")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", 7, head, "main")
    attempt = await ws.merge(path, "main")
    assert attempt.merge_sha == main
    assert (attempt.conflicts, attempt.steering) == (["loader.py"], [])
    assert (path / "AGENTS.md").read_text() == "main's newer rules\n"
    await ws.release(path)


async def test_a_conflict_in_agent_instructions_is_left_to_a_person(
    tmp_path: Path,
) -> None:
    forge, work, _ = _repo_with_pr(tmp_path)
    (work / "AGENTS.md").write_text("the PR's rules\n")
    head = _commit(work, "one\n", "pr change")
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    git(work, "checkout", "-q", "main")
    (work / "AGENTS.md").write_text("main's rules\n")
    _commit(work, "one\n", "main change")
    git(work, "push", "-q", "origin", "main")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", 7, head, "main")
    attempt = await ws.merge(path, "main")
    assert attempt.steering == ["AGENTS.md"]
    assert (path / "AGENTS.md").read_text() == "main's rules\n", (
        "no agent reads the PR's side"
    )
    await ws.release(path)


async def test_a_merge_git_refuses_is_not_read_as_a_clean_one(tmp_path: Path) -> None:
    forge, work, _ = _repo_with_pr(tmp_path)
    head = _commit(work, "one, from the PR\n", "pr change")
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    git(work, "checkout", "-q", "--orphan", "other")
    git(work, "rm", "-q", "-rf", ".")
    (work / "else.txt").write_text("unrelated\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "unrelated history")
    git(work, "push", "-q", "origin", "other")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", 7, head, "main")
    with pytest.raises(RuntimeError, match="could not merge"):
        await ws.merge(path, "other")
    await ws.release(path)


async def test_an_agents_changes_leave_out_the_instructions_the_checkout_swapped_in(
    tmp_path: Path,
) -> None:
    """A fix's patch holds what the agent changed, not main's instruction files the
    checkout carries for it (nor the removal of the PR's own)."""
    forge, work, _ = _repo_with_pr(tmp_path)
    (work / "AGENTS.md").write_text("the PR's rules\n")
    (work / "CLAUDE.md").write_text("added by the PR\n")
    head = _commit(work, "one, from the PR\n", "pr change")
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    git(work, "checkout", "-q", "main")
    (work / "AGENTS.md").write_text("main's rules\n")
    _commit(work, "one\n", "main change")
    git(work, "push", "-q", "origin", "main")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", 7, head, "main")
    assert (path / "AGENTS.md").read_text() == "main's rules\n"
    assert not (path / "CLAUDE.md").exists()
    (path / "loader.py").write_text("fixed\n")
    changes = await ws.changes(path)
    assert changes.files == ["loader.py"]
    assert "AGENTS.md" not in changes.patch and "CLAUDE.md" not in changes.patch
    await ws.release(path)


async def test_git_config_an_agent_points_at_never_runs_on_the_host(
    tmp_path: Path,
) -> None:
    """An agent writes its checkout and its git metadata folder: a ``.git`` file or a
    ``commondir`` naming a folder whose config sets ``core.fsmonitor`` must not make
    the host's own git commands there run it."""
    forge = tmp_path / "forge"
    upstream = forge / "acme" / "widget.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(upstream), str(work))
    head = _commit(work, "one\n", "seed")
    git(work, "push", "-q", "origin", "HEAD:main")

    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    path = await ws.prepare("acme/widget", None, head, "main")
    admin = Path((path / ".git").read_text().split(":", 1)[1].strip())
    ran = tmp_path / "ran"
    evil = path / "evil"
    git(tmp_path, "init", "-q", "--bare", str(evil))
    with (evil / "config").open("a") as config:
        config.write(f"[core]\n\tfsmonitor = touch {ran}\n\thooksPath = {evil}\n")
    (path / ".git").write_text(f"gitdir: {evil}\n")
    (admin / "commondir").write_text(f"{evil}\n")
    (path / "loader.py").write_text("two\n")

    ws2 = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")  # a restart
    for workspace in (ws, ws2):
        changes = await workspace.changes(path)
        assert "loader.py" in changes.files and changes.base_sha == head
        assert not ran.exists(), "the agent's fsmonitor never ran"
    await ws2.release(path)
    assert not path.exists()


async def test_a_change_applied_to_a_new_checkout_gives_the_same_diff(
    tmp_path: Path,
) -> None:
    """A conversation about a fix goes on from the change as it stands: its patch
    applied to a new checkout of the same commit is that patch again, binary files
    and new files included."""
    forge, work, _ = _repo_with_pr(tmp_path)
    head = _commit(work, "one, from the PR\n", "pr change")
    git(work, "push", "-q", "origin", "feature:refs/pull/7/head")
    ws = GitWorkspace(tmp_path / "data", forge_url=f"file://{forge}")
    first = await ws.prepare("acme/widget", 7, head, "main")
    (first / "loader.py").write_text("one, fixed\n")
    (first / "new.bin").write_bytes(bytes(range(256)))
    made = await ws.changes(first)
    await ws.release(first)

    again = await ws.prepare("acme/widget", 7, head, "main")
    await ws.apply(again, made.patch)
    assert (again / "new.bin").read_bytes() == bytes(range(256))
    redone = await ws.changes(again)
    assert redone.patch == made.patch and redone.files == ["loader.py", "new.bin"]
    with pytest.raises(RuntimeError):
        await ws.apply(again, made.patch)  # already applied: it does not apply twice
    await ws.release(again)
