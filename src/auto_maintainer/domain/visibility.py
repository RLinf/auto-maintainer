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

"""Public and internal text.

``PublicText`` is the only text type that forge write methods accept. It can only
be created by the leak guard in ``policy.leak_guard``, which holds ``GUARD_KEY``;
``tests/test_layers.py`` checks that no other module imports the key.
"""

from __future__ import annotations

from enum import StrEnum


class Visibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"


GUARD_KEY = object()


class PublicText:
    """Text that passed the leak guard for one repository and may be posted."""

    __slots__ = ("repo", "text")

    def __init__(self, text: str, repo: str, *, _key: object) -> None:
        if _key is not GUARD_KEY:
            raise TypeError("PublicText is created only by policy.leak_guard")
        self.text = text
        self.repo = repo

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"PublicText({self.text[:40]!r}..., repo={self.repo!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicText) and (self.text, self.repo) == (
            other.text,
            other.repo,
        )

    def __hash__(self) -> int:
        return hash((self.text, self.repo))


class InternalText(str):
    """Text for maintainers only: scores, tiers, rubric content, partner notes."""

    __slots__ = ()
