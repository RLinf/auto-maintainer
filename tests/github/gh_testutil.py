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

"""Helpers for the GitHub adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

from auto_maintainer.domain.visibility import GUARD_KEY, PublicText

API = "https://api.github.com"
FIXTURES = Path(__file__).parent / "fixtures"


async def no_sleep(_seconds: float) -> None:
    return None


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def public(text: str, repo: str = "acme/widget") -> PublicText:
    """PublicText as the leak guard would build it; tests stand in for the guard."""
    return PublicText(text, repo, _key=GUARD_KEY)
