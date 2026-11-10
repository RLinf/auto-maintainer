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

"""Where a review may comment inline: the lines each hunk of a unified diff shows.

A forge takes an inline comment only on lines its diff of the change shows, the
changed lines and their context, and refuses the whole review otherwise (GitHub
answers 422). ``commentable`` reads those ranges from a unified diff (``git diff``,
or the forge's own diff of a PR); ``fits`` says whether a comment's lines lie inside
one of them.
"""

from __future__ import annotations

import re
from typing import Literal

Side = Literal["LEFT", "RIGHT"]
Ranges = dict[str, dict[str, list[tuple[int, int]]]]
"""Per path, per side (``RIGHT`` new lines, ``LEFT`` old lines): the line ranges the
diff's hunks show, inclusive."""

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(text: str, prefix: str) -> str | None:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if text == "/dev/null":
        return None
    return text[len(prefix) :] if text.startswith(prefix) else text


def commentable(diff: str) -> Ranges:
    """The ranges each file's hunks show in ``diff``. A file the diff names without
    hunks (a binary file, a rename alone) has none; a file it does not reach (a
    diff cut short) is absent."""
    out: Ranges = {}
    old: str | None = None
    new: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old = new = None
            continue
        if line.startswith("--- "):
            old = _path(line[4:], "a/")
            continue
        if line.startswith("+++ "):
            new = _path(line[4:], "b/")
            for path in (old, new):
                if path is not None:
                    out.setdefault(path, {"LEFT": [], "RIGHT": []})
            continue
        m = _HUNK.match(line)
        if m is None:
            continue
        left, left_n, right, right_n = (
            int(m.group(1)),
            int(m.group(2) if m.group(2) is not None else 1),
            int(m.group(3)),
            int(m.group(4) if m.group(4) is not None else 1),
        )
        if new is not None and right_n > 0:
            out[new]["RIGHT"].append((right, right + right_n - 1))
        if old is not None and left_n > 0:
            out[old]["LEFT"].append((left, left + left_n - 1))
    return out


def fits(
    ranges: Ranges,
    path: str,
    line: int,
    start_line: int | None = None,
    side: Side = "RIGHT",
) -> bool | None:
    """Whether lines ``start_line``..``line`` of ``path`` lie inside one hunk on
    ``side``; ``None`` when the diff does not reach ``path``, so nothing is known."""
    sides = ranges.get(path)
    if sides is None:
        return None
    start = start_line or line
    return any(lo <= start and line <= hi for lo, hi in sides.get(side, []))


def hunk_of(
    ranges: Ranges, path: str, line: int, side: Side = "RIGHT"
) -> tuple[int, int] | None:
    """The hunk on ``side`` of ``path`` that shows ``line``, if any."""
    for lo, hi in ranges.get(path, {}).get(side, []):
        if lo <= line <= hi:
            return lo, hi
    return None
