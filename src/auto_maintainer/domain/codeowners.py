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

"""CODEOWNERS parsing with gitignore-style patterns; the last matching rule wins."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import forges

CODEOWNERS_LOCATIONS = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    ".gitlab/CODEOWNERS",
)
"""Every place a forge looks for CODEOWNERS, for reading all candidates; one
repository's own order is ``codeowners_locations``."""


def codeowners_locations(repo: str) -> tuple[str, ...]:
    """Where the forge ``repo`` is on looks for CODEOWNERS, first to last."""
    return forges.info_of(repo).codeowners


@dataclass(frozen=True)
class OwnerRule:
    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    directory = p.endswith("/")
    p = p.rstrip("/")
    out = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += r".*"
            i += 2
        elif p[i] == "*":
            out += r"[^/]*"
            i += 1
        elif p[i] == "?":
            out += r"[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    prefix = "^" if anchored else r"^(?:.*/)?"
    suffix = r"/.*$" if directory else r"(?:/.*)?$"
    return re.compile(prefix + out + suffix)


def parse_codeowners(text: str | None) -> list[OwnerRule]:
    rules = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rules.append(
            OwnerRule(
                parts[0], tuple(o.lstrip("@") for o in parts[1:]), _compile(parts[0])
            )
        )
    return rules


def owners_for(rules: list[OwnerRule], path: str) -> tuple[str, ...]:
    owners: tuple[str, ...] = ()
    for rule in rules:
        if rule.regex.match(path):
            owners = rule.owners
    return owners


def owners_of_paths(rules: list[OwnerRule], paths: list[str]) -> list[str]:
    """Distinct owners of any of ``paths``, in first-seen order, without the leading ``@``."""
    seen: list[str] = []
    for path in paths:
        for owner in owners_for(rules, path):
            if owner not in seen:
                seen.append(owner)
    return seen
