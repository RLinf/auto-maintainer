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

"""An agent's answer as text the service shows or posts: shortened from its end, at
a paragraph or line, since an answer leads with its structure (a heading, the first
file it changed); and with links to files of its checkout, which mean nothing off
the service's host, written as the files' paths in the repository."""

from __future__ import annotations

import re
from pathlib import Path

ELLIPSIS = "\n\n…"
"""What marks a shortened answer: the whole one is in the job's live view."""


def clip(text: str, limit: int) -> str:
    """``text`` (stripped) from its start, at most ``limit`` characters: cut at the
    last paragraph break before the limit, else the last line break, else the last
    space, and marked with ``ELLIPSIS``. Cutting from the front (``text[-n:]``) lost
    an answer's heading and first files, and began mid-word."""
    text = text.strip()
    if len(text) <= limit:
        return text
    room = max(0, limit - len(ELLIPSIS))
    cut = text[:room]
    for sep in ("\n\n", "\n", " "):
        at = cut.rfind(sep)
        if at > room // 2:
            cut = cut[:at]
            break
    return cut.rstrip() + ELLIPSIS


_LINK = re.compile(r"\[([^\]\n]*)\]\(<?file://(/[^)\s>]+)>?\)")
_BARE = re.compile(r"file://(/[^\s)>\]\"'`]+)")
_LINES = re.compile(r"^L(\d+)(?:-L?(\d+))?$")


def _repo_path(target: str, root: str) -> str | None:
    """``target`` (a host path, with an optional ``#L1-L2``) as ``path:1-2`` in the
    repository whose checkout is at ``root``; ``None`` outside it."""
    if not target.startswith(root):
        return None
    rel, _, fragment = target[len(root) :].partition("#")
    lines = _LINES.match(fragment)
    if lines:
        rel += f":{lines.group(1)}" + (f"-{lines.group(2)}" if lines.group(2) else "")
    return rel


def checkout_paths(text: str, workdir: object) -> str:
    """``text`` with each ``file://`` link into the checkout at ``workdir`` written
    as the file's repository path in code (``src/app.py:907-936``), after its label
    when that says more than the file's name; a ``file://`` link elsewhere on the
    host keeps only its label, and a bare ``file://`` path into the checkout becomes
    its repository path."""
    root = str(Path(str(workdir))).rstrip("/") + "/"

    def link(m: re.Match[str]) -> str:
        label = m.group(1).strip()
        rel = _repo_path(m.group(2), root)
        if rel is None:
            return label
        ref = f"`{rel}`"
        bare = label.strip("`")
        path = rel.split(":", 1)[0]
        if not bare or bare in (rel, path, path.rsplit("/", 1)[-1]):
            return ref
        return f"{label} ({ref})"

    def bare(m: re.Match[str]) -> str:
        rel = _repo_path(m.group(1), root)
        return rel if rel is not None else m.group(0)

    return _BARE.sub(bare, _LINK.sub(link, text))
