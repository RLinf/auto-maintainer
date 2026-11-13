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


"""Minor conflicts with the base branch (design 5.5, merge scheduling): which kind a
conflicted file is, and whether a resolution only combines what both sides wrote.

The merge schedule resolves a PR's conflict with its base by itself only when every
conflicted file is of a kind the repository enabled (``ResolveKind``: docs, README,
install-target lists by default), and, unless the repository turns it off, only when
the resolution passes ``union_problem``: it writes no word that neither side has, and
keeps everything either side added. Anything else is a person's to resolve.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .profile import ResolveKind

MARKERS = ("<<<<<<< ", ">>>>>>> ")
"""The lines git puts around a conflict; a resolved file has none left. (Its
``=======`` line alone also underlines a reStructuredText heading.)"""


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    """``pattern`` as a regular expression over a whole path: ``**/`` any folders
    (or none), ``**`` anything, ``*`` anything within one folder, ``?`` one
    character of a name."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    """Whether ``path`` (relative, ``/``-separated) matches the glob ``pattern``."""
    return bool(_glob(pattern.strip().lstrip("/")).match(path))


def kind_of(path: str, kinds: list[ResolveKind]) -> ResolveKind | None:
    """The first enabled kind whose paths match ``path``, or ``None``."""
    for kind in kinds:
        if kind.enabled and any(glob_match(p, path) for p in kind.paths):
            return kind
    return None


_WORD = re.compile(r"\w[\w.+-]*|[^\w\s]")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text))


def _lines(text: str | None) -> list[str]:
    return [line.rstrip() for line in (text or "").splitlines()]


def unresolved(text: str) -> bool:
    """Whether ``text`` still has a conflict marker at the start of a line."""
    return any(line.startswith(MARKERS) for line in text.splitlines())


def union_problem(
    base: str | None, ours: str | None, theirs: str | None, resolved: str
) -> str | None:
    """Why ``resolved`` is more than a combination of the two sides of a conflict, or
    ``None`` when it is one. ``ours`` and ``theirs`` are the file on each side
    (``None`` when that side deleted it), ``base`` the file where they parted.

    A resolution may keep any line of either side and may join the two sides'
    versions of a line (two install targets added to one list), but it writes no
    word that neither side has, and every line either side added keeps its words.
    """
    if unresolved(resolved):
        return "conflict markers remain"
    if ours is None or theirs is None:
        return "one side deleted the file"
    mine, other, before = _lines(ours), _lines(theirs), _lines(base)
    known = set(mine) | set(other)
    vocabulary = _words(ours) | _words(theirs)
    after = _lines(resolved)
    for line in after:
        if not line.strip() or line in known:
            continue
        new = _words(line) - vocabulary
        if new:
            return (
                f"it writes {', '.join(sorted(new)[:5])}, which neither side has "
                f"(in {line.strip()[:80]!r})"
            )
    kept = _words(resolved)
    for line in (set(mine) | set(other)) - set(before):
        if not line.strip():
            continue
        lost = _words(line) - kept
        if lost:
            return (
                f"it drops {', '.join(sorted(lost)[:5])}, which one side added "
                f"(in {line.strip()[:80]!r})"
            )
    return None
