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

"""The approved profile lock (design 2.2): the only profile the service acts on."""

from __future__ import annotations

import difflib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain.profile import RepoProfile
from .layers import MergedProfile, ProfileError, dump_yaml, parse_yaml

LOCK_SCHEMA = 1


class ProfileLock(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(LOCK_SCHEMA, alias="schema")
    repo: str
    hash: str
    layers: dict[str, str]
    approved_by: list[str]
    approved_at: datetime
    source: str
    """What produced the change: ``onboarding``, ``dashboard``, ``in_repo``, ``release``, ``memory``."""
    profile: dict[str, Any]

    def to_profile(self) -> RepoProfile:
        return RepoProfile.model_validate(self.profile)

    def to_yaml(self) -> str:
        return dump_yaml(self.model_dump(mode="json", by_alias=True))


def lock_from(
    merged: MergedProfile, approved_by: list[str], approved_at: datetime, source: str
) -> ProfileLock:
    return ProfileLock(
        repo=merged.profile.repo,
        hash=merged.hash,
        layers=merged.layer_hashes,
        approved_by=sorted(set(approved_by)),
        approved_at=approved_at,
        source=source,
        profile=merged.data,
    )


def parse_lock(text: str | None, source: str) -> ProfileLock | None:
    if not text:
        return None
    data = parse_yaml(text, source)
    if data.get("schema", LOCK_SCHEMA) != LOCK_SCHEMA:
        raise ProfileError(
            f"{source}: lock schema {data.get('schema')} is not supported"
        )
    return ProfileLock.model_validate(data)


def profile_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> str:
    """A unified diff between two profiles' YAML forms."""
    before = dump_yaml(old).splitlines() if old else []
    after = dump_yaml(new).splitlines()
    return "\n".join(
        difflib.unified_diff(before, after, "approved", "candidate", lineterm="")
    )
