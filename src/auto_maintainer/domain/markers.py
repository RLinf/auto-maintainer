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

"""HTML markers that let the service find its own comments again.

Every public comment the service posts starts with a marker such as
``<!-- auto-maintainer: v=1 kind=review pr=42 sha=<sha> action=<id> -->``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER_RE = re.compile(r"<!--\s*auto-maintainer:\s*(.*?)\s*-->", re.DOTALL)
_PAIR_RE = re.compile(r"(\w+)=(\S+)")

REVIEW_KINDS = frozenset({"review", "followup"})


@dataclass(frozen=True)
class Marker:
    kind: str
    sha: str | None = None
    pr: int | None = None
    action: str | None = None
    legacy: bool = False


def build_marker(kind: str, pr: int | None, sha: str | None, action: str | None) -> str:
    parts = ["v=1", f"kind={kind}"]
    if pr is not None:
        parts.append(f"pr={pr}")
    if sha:
        parts.append(f"sha={sha}")
    if action:
        parts.append(f"action={action}")
    return f"<!-- auto-maintainer: {' '.join(parts)} -->"


def parse_markers(body: str, legacy_patterns: list[str] | None = None) -> list[Marker]:
    """Return the service markers in ``body``, then any legacy review markers."""
    found = []
    for m in _MARKER_RE.finditer(body or ""):
        fields = dict(_PAIR_RE.findall(m.group(1)))
        pr = fields.get("pr")
        found.append(
            Marker(
                kind=fields.get("kind", ""),
                sha=fields.get("sha"),
                pr=int(pr) if pr and pr.isdigit() else None,
                action=fields.get("action"),
            )
        )
    for pattern in legacy_patterns or []:
        for m in re.finditer(pattern, body or ""):
            sha = m.group(1) if m.groups() else None
            found.append(Marker(kind="review", sha=sha, legacy=True))
    return found


def has_marker(
    body: str, *, kind: str | None = None, action: str | None = None
) -> bool:
    for marker in parse_markers(body):
        if (kind is None or marker.kind == kind) and (
            action is None or marker.action == action
        ):
            return True
    return False
