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
