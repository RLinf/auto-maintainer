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

"""Memory changes, tiers, signals, and the memory repo layout (design 5.11, 7.2)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .clock import utc_now
from .forges import memory_dir, repo_of_memory_path
from .ids import new_id


class Tier(StrEnum):
    T0 = "T0"
    """Journal: committed directly."""
    T1 = "T1"
    """Working knowledge: merged after a notice window when the repository allows it."""
    T2 = "T2"
    """Configuration: merged only after a maintainer of each affected repository approves."""


class MemoryChangeState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class MemoryChange(BaseModel):
    """An edit to the memory repo, applied on a branch and merged by the memory gate."""

    tier: Tier
    title: str
    description: str = ""
    files: dict[str, str | None]
    """Path to new content; ``None`` deletes the file."""
    repos: list[str] = Field(default_factory=list)
    """Target repositories whose directories or configuration the change affects."""
    service: bool = False
    """The change edits the service's own settings (``config/service.yaml``). It needs
    the approval of a maintainer of the service's repository, recorded in
    ``approvals`` under that repository, instead of one per target repository."""
    author: str = "service"
    requested_by: str | None = None
    """A maintainer who asked for the change, for example in chat."""
    from_own_signals: bool = True
    """Whether the change comes from the affected repository's own signals."""
    state: MemoryChangeState = MemoryChangeState.OPEN
    approvals: dict[str, list[str]] = Field(default_factory=dict)
    """Repository to the logins that approved for it."""
    branch: str | None = None
    pr_number: int | None = None
    id: str = Field(default_factory=lambda: new_id("mem"))
    created_at: datetime = Field(default_factory=utc_now)
    merged_at: datetime | None = None
    rejected_reason: str | None = None


class SignalKind(StrEnum):
    PRIORITY_OVERRIDE = "priority_override"
    DRAFT_EDITED = "draft_edited"
    PROPOSAL_REJECTED = "proposal_rejected"
    FINDING_FATE = "finding_fate"
    PR_OUTCOME = "pr_outcome"
    CI_OUTCOME = "ci_outcome"
    CHAT_INSTRUCTION = "chat_instruction"
    HUMAN_ACTION = "human_action"


class Signal(BaseModel):
    kind: SignalKind
    repo: str
    number: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "service"
    id: str = Field(default_factory=lambda: new_id("sig"))
    created_at: datetime = Field(default_factory=utc_now)
    consumed_by: str | None = None


def repo_dir(repo: str) -> str:
    """The repository's directory in the memory repo (``forges.memory_dir``)."""
    return memory_dir(repo)


def profile_path(repo: str) -> str:
    return f"{repo_dir(repo)}/profile.yaml"


def lock_path(repo: str) -> str:
    return f"{repo_dir(repo)}/profile.lock.yaml"


def journal_path(repo: str, when: datetime) -> str:
    return f"{repo_dir(repo)}/journal/{when:%Y/%m/%d}.jsonl"


CONFIG_PATH_SUFFIXES = ("profile.yaml", "profile.lock.yaml", "rubric/rubric.yaml")
"""Paths under a repository directory that are configuration, not knowledge."""


def is_configuration_path(path: str) -> bool:
    """Whether a memory path is configuration (T2) rather than knowledge."""
    if path.startswith("config/") or path.startswith("packs/"):
        return True
    return repo_of_memory_path(path) is not None and path.endswith(CONFIG_PATH_SUFFIXES)


def repos_of_path(path: str) -> list[str]:
    """The target repository a memory path belongs to, if any."""
    found = repo_of_memory_path(path)
    return [found] if found else []
