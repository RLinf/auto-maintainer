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

"""Repository permission levels, ordered from least to most."""

from __future__ import annotations

from enum import StrEnum

_ORDER = ["none", "read", "triage", "write", "maintain", "admin"]


class Permission(StrEnum):
    """A user's role on a repository, as the forge reports it."""

    NONE = "none"
    READ = "read"
    TRIAGE = "triage"
    WRITE = "write"
    MAINTAIN = "maintain"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ORDER.index(self.value)

    def at_least(self, other: Permission) -> bool:
        """Return whether this level is ``other`` or higher."""
        return self.rank >= Permission(other).rank

    @classmethod
    def parse(cls, value: str | None) -> Permission:
        """Map a forge role name to a level; unknown names map to ``NONE``."""
        aliases = {"pull": "read", "push": "write"}
        value = aliases.get((value or "").lower(), (value or "").lower())
        return cls(value) if value in _ORDER else cls.NONE
