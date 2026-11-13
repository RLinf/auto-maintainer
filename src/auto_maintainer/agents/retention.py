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

"""How long the agents' files stay on the host (design 7.3): each attempt's run folder
under ``<data>/runs``, and the old sessions the agent CLIs keep in their home folders
(``ExecRuntime.session_paths``).

Agents write both (a container mounts its run folder and its CLI's home paths), so
nothing here follows a link: a link is never a match, never descended into, and only
ever removed itself."""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

RUNS_KEPT = timedelta(days=7)
"""A run folder in which nothing changed for this long is removed."""
SESSIONS_KEPT = timedelta(days=14)
"""A CLI's session in which nothing changed for this long is removed."""


def matches(root: Path, pattern: str) -> list[Path]:
    """Entries under ``root`` that match ``pattern`` (``/``-separated names, each an
    ``fnmatch`` pattern), reached without following a link, and not links themselves."""
    level = [root]
    for part in pattern.split("/"):
        found = []
        for folder in level:
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for name in names:
                path = folder / name
                if fnmatch.fnmatchcase(name, part) and not path.is_symlink():
                    found.append(path)
        level = found
    return level


def changed_since(path: Path, cutoff: float) -> bool:
    """Whether ``path`` or anything under it was modified after ``cutoff`` (seconds
    since the epoch), without following links."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if info.st_mtime > cutoff:
        return True
    if not stat.S_ISDIR(info.st_mode):
        return False
    for folder, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            try:
                if os.lstat(os.path.join(folder, name)).st_mtime > cutoff:
                    return True
            except OSError:
                continue
    return False


def prune(root: Path, patterns: Iterable[str], kept: timedelta, now: float) -> int:
    """Remove each match of ``patterns`` under ``root`` in which nothing changed for
    ``kept`` before ``now`` (seconds since the epoch). Returns how many it removed."""
    cutoff = now - kept.total_seconds()
    removed = 0
    for pattern in patterns:
        for path in matches(root, pattern):
            if changed_since(path, cutoff):
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
    return removed


def prune_agent_files(
    runs_dir: Path, homes: Iterable[Path], session_paths: Iterable[str], now: float
) -> int:
    """Remove run folders older than ``RUNS_KEPT``, and the sessions ``session_paths``
    name in each of ``homes`` older than ``SESSIONS_KEPT``. Returns how many."""
    patterns = sorted(set(session_paths))
    removed = prune(runs_dir, ["*"], RUNS_KEPT, now)
    for home in homes:
        removed += prune(home, patterns, SESSIONS_KEPT, now)
    return removed
