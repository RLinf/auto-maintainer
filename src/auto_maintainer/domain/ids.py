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

"""Identifiers for repositories, pull requests, and service records."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from . import forges

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def check_repo(name: str) -> str:
    """``name`` in its canonical form when it names a repository (``owner/repo`` on
    the default forge, ``forge:group/project`` elsewhere; ``domain/forges.py``), else
    raise ``ValueError``."""
    return forges.check_repo(name)


def check_login(login: str) -> str:
    """``login`` in its canonical form when it can be someone's login (bare on the
    default forge, ``forge:login`` elsewhere), else raise ``ValueError``."""
    return forges.check_person(login)


def is_sha(value: str) -> bool:
    """Return whether ``value`` looks like an abbreviated or full commit SHA."""
    return bool(_SHA_RE.match(value or ""))


def short_sha(sha: str | None) -> str:
    """Return the seven-character form of ``sha``, or ``-`` when there is none."""
    return sha[:7] if sha else "-"


def same_commit(a: str | None, b: str | None) -> bool:
    """Return whether two SHAs, either possibly abbreviated, name the same commit."""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n].lower() == b[:n].lower()


def new_id(prefix: str) -> str:
    """Return a random record ID such as ``act_3f2a9c1d0b7e4a51``."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, order=True)
class PrRef:
    """A pull request or issue in a repository."""

    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @classmethod
    def parse(cls, text: str) -> PrRef:
        """Parse ``owner/repo#123``."""
        repo, _, num = text.partition("#")
        if not num.isdigit():
            raise ValueError(f"expected 'owner/repo#number', got {text!r}")
        return cls(check_repo(repo), int(num))
