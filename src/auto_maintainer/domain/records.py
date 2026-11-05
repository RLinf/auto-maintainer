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

"""Operational records the store keeps: jobs, repositories, audit entries, and so on."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .actions import Action, ActionStatus, Decision
from .clock import utc_now
from .events import EventKind
from .ids import new_id


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WaitSpec(BaseModel):
    """What wakes a waiting job: an event of one of ``kinds`` about its target, or the deadline."""

    kinds: list[EventKind]
    repo: str | None = None
    number: int | None = None
    action_ids: list[str] = Field(default_factory=list)
    deadline: datetime
    reason: str = ""


class Job(BaseModel):
    playbook: str
    key: str
    repo: str | None = None
    number: int | None = None
    pool: str = "io"
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    events: list[dict[str, Any]] = Field(default_factory=list)
    """The events that triggered or woke the job, newest last."""
    wait: WaitSpec | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    lease_until: datetime | None = None
    not_before: datetime | None = None
    id: str = Field(default_factory=lambda: new_id("job"))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RepoRecord(BaseModel):
    repo: str
    active: bool = False
    """Whether the repository has an approved profile lock and is being served."""
    paused: bool = False
    added_by: str = ""
    added_at: datetime = Field(default_factory=utc_now)
    last_poll_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    watched_files: dict[str, str] = Field(default_factory=dict)
    """Path to content hash, for drift detection."""


class ActionRecord(BaseModel):
    action: Action
    decision: Decision | None = None
    status: ActionStatus = ActionStatus.NEW
    external_ref: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    approver: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEntry(BaseModel):
    kind: str
    """``decision``, ``execution``, ``proposal``, ``config``, ``memory``, ``pause``, ..."""
    repo: str | None = None
    number: int | None = None
    actor: str = "service"
    action_id: str | None = None
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: new_id("aud"))
    created_at: datetime = Field(default_factory=utc_now)


class AgentRunRecord(BaseModel):
    id: str
    job_id: str | None = None
    repo: str | None = None
    runtime: str
    task_kind: str
    ok: bool
    permissions: str = "restricted"
    error: str | None = None
    session: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    transcript_path: str | None = None
    duration_seconds: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)


class ReviewRecord(BaseModel):
    """A review or follow-up the service posted, with its structured findings."""

    repo: str
    number: int
    sha: str
    kind: str
    action_id: str
    result: dict[str, Any]
    external_id: int | None = None
    target: str = "review"
    created_at: datetime = Field(default_factory=utc_now)


class CiObservation(BaseModel):
    repo: str
    number: int | None = None
    sha: str
    run_id: int | None = None
    check_id: int
    check_name: str
    workflow: str | None = None
    category: str
    signature: str | None = None
    confidence: float = 0.0
    explanation: str = ""
    rerun: bool = False
    id: str = Field(default_factory=lambda: new_id("ci"))
    created_at: datetime = Field(default_factory=utc_now)


class ChatThread(BaseModel):
    owner: str
    title: str = ""
    repo: str | None = None
    session: str | None = None
    runtime: str | None = None
    id: str = Field(default_factory=lambda: new_id("chat"))
    created_at: datetime = Field(default_factory=utc_now)


class ChatMessage(BaseModel):
    thread_id: str
    role: str
    """``user``, ``assistant``, or ``tool``."""
    text: str
    proposals: list[str] = Field(default_factory=list)
    id: str = Field(default_factory=lambda: new_id("msg"))
    created_at: datetime = Field(default_factory=utc_now)


class ProfileState(BaseModel):
    """The store's mirror of a repository's lock and pending candidate."""

    repo: str
    lock_hash: str | None = None
    candidate: dict[str, Any] | None = None
    candidate_hash: str | None = None
    candidate_layers: dict[str, str] = Field(default_factory=dict)
    candidate_error: str | None = None
    candidate_diff: str | None = None
    approvals: list[str] = Field(default_factory=list)
    doctor: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)
