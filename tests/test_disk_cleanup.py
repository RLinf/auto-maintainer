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

"""Keeping the data folder's size bounded: conversations' idle checkouts, checkouts
nothing uses, old run folders, and the agent CLIs' old sessions (design 7.3)."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from auto_maintainer.agents.registry import ADAPTERS
from auto_maintainer.agents.retention import prune_agent_files
from auto_maintainer.domain.records import ChatMessage, ChatThread
from auto_maintainer.testing.harness import Harness
from auto_maintainer.workspace.git import GitWorkspace

DAY = 86_400

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


def age(path: Path, days: float) -> None:
    """Make ``path`` and everything under it last changed ``days`` ago."""
    when = time.time() - days * DAY
    for folder, dirs, files in os.walk(path, topdown=False):
        for name in [*dirs, *files]:
            os.utime(os.path.join(folder, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


async def test_an_idle_conversation_gives_up_its_checkout(harness: Harness) -> None:
    h = harness
    now = h.clock.now()
    threads = {}
    for name, last, live in [
        ("idle", now - timedelta(days=8), None),
        ("recent", now - timedelta(days=1), None),
        ("answering", now - timedelta(days=8), "run_1"),
    ]:
        thread = ChatThread(
            owner="maria",
            workdir=f"/data/repos/acme/widget/{name}",
            session=f"s-{name}",
            live_run=live,
            created_at=now - timedelta(days=30),
        )
        await h.store.save_thread(thread)
        await h.store.add_message(
            ChatMessage(thread_id=thread.id, role="user", text="hi", created_at=last)
        )
        threads[name] = thread
    silent = ChatThread(
        owner="maria", workdir="/data/repos/acme/widget/silent", session="s"
    )
    silent.created_at = now - timedelta(days=9)
    await h.store.save_thread(silent)
    await h.store.save_thread(ChatThread(owner="maria"))
    kept: list[frozenset[Path]] = []

    async def cleanup(keep: frozenset[Path]) -> None:
        kept.append(keep)

    h.service.a.cleanup = cleanup
    await h.service.clean_disk()

    assert sorted(h.service.a.workspace.released) == [  # type: ignore[attr-defined]
        Path("/data/repos/acme/widget/idle"),
        Path("/data/repos/acme/widget/silent"),
    ]
    idle = await h.store.get_thread(threads["idle"].id)
    assert idle is not None and idle.workdir is None and idle.session is None
    recent = await h.store.get_thread(threads["recent"].id)
    assert recent is not None and recent.session == "s-recent"
    assert kept == [
        frozenset(
            {
                Path("/data/repos/acme/widget/recent"),
                Path("/data/repos/acme/widget/answering"),
            }
        )
    ]


async def test_the_daily_schedule_runs_the_cleanup(harness: Harness) -> None:
    assert "disk_cleanup" in harness.service.scheduler.schedules
    assert harness.service.scheduler.builtins["disk_cleanup"] == (
        harness.service.clean_disk
    )


async def test_sweep_removes_only_checkouts_nothing_uses(tmp_path: Path) -> None:
    forge = tmp_path / "forge"
    upstream = forge / "acme" / "widget.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(upstream), str(work))
    (work / "a.txt").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "seed")
    git(work, "push", "-q", "origin", "HEAD:main")
    sha = git(work, "rev-parse", "HEAD")
    data = tmp_path / "data"
    before = GitWorkspace(data, forge_url=f"file://{forge}")
    orphan = await before.prepare("acme/widget", None, sha, "main")
    talk = await before.prepare("acme/widget", None, sha, "main")
    fresh = await before.prepare("acme/widget", None, sha, "main")
    for path in (orphan, talk):
        age(path, 2)

    # The service started again: nothing it prepared before is held.
    after = GitWorkspace(data, forge_url=f"file://{forge}")
    held = await after.prepare("acme/widget", None, sha, "main")
    age(held, 2)
    assert await after.sweep({talk}, older_than=timedelta(days=1)) == 1

    assert not orphan.exists()
    assert talk.is_dir() and fresh.is_dir() and held.is_dir()
    listed = git(after.mirror("acme/widget"), "worktree", "list")
    assert str(orphan) not in listed and str(talk) in listed


def test_old_run_folders_and_sessions_go(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "old" / "home").mkdir(parents=True)
    (runs / "old" / "transcript.jsonl").write_text("{}")
    (runs / "busy").mkdir()
    (runs / "busy" / "transcript.jsonl").write_text("{}")
    age(runs / "old", 8)
    age(runs / "busy", 8)
    (runs / "busy" / "transcript.jsonl").touch()

    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-data-repos-acme"
    (project / "memory").mkdir(parents=True)
    (project / "memory" / "MEMORY.md").write_text("kept")
    (project / "old.jsonl").write_text("{}")
    (project / "new.jsonl").write_text("{}")
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    (brain / "conv-old").mkdir(parents=True)
    (brain / "conv-old" / "plan.md").write_text("x")
    (home / ".claude" / ".credentials.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.jsonl").write_text("{}")
    (home / ".claude" / "projects" / "link").symlink_to(outside)
    (home / ".claude" / "todos").mkdir()
    (home / ".claude" / "todos" / "x").symlink_to(outside / "precious.jsonl")
    age(home, 15)
    age(outside, 15)
    (project / "new.jsonl").touch()

    sessions = [p for cls in ADAPTERS.values() for p in cls.session_paths]
    assert prune_agent_files(runs, [home], sessions, time.time()) == 3

    assert sorted(p.name for p in runs.iterdir()) == ["busy"]
    assert not (project / "old.jsonl").exists()
    assert (project / "new.jsonl").exists()
    assert (project / "memory" / "MEMORY.md").exists()
    assert not (brain / "conv-old").exists()
    assert (home / ".claude" / ".credentials.json").exists()
    assert (outside / "precious.jsonl").exists()
    assert (home / ".claude" / "todos" / "x").is_symlink()


def test_session_paths_lie_in_the_clis_own_home_paths() -> None:
    for name, cls in ADAPTERS.items():
        for pattern in cls.session_paths:
            assert any(pattern.startswith(f"{home}/") for home in cls.home_paths), (
                name,
                pattern,
            )
