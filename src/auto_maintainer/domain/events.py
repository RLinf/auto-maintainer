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

"""Events: things that happened, which the engine turns into jobs."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .clock import utc_now
from .ids import new_id


class EventKind(StrEnum):
    PR_OPENED = "pr.opened"
    PR_SYNCHRONIZED = "pr.synchronized"
    PR_UPDATED = "pr.updated"
    PR_LABELED = "pr.labeled"
    PR_REVIEW_SUBMITTED = "pr.review_submitted"
    PR_COMMENT = "pr.comment"
    PR_CLOSED = "pr.closed"
    PR_RECONCILED = "pr.reconciled"
    CHECKS_COMPLETED = "checks.completed"
    ISSUE_OPENED = "issue.opened"
    REPO_ADDED = "repo.added"
    REPO_FILES_CHANGED = "repo.files_changed"
    SCHEDULE_TICK = "schedule.tick"
    DASHBOARD_COMMAND = "dashboard.command"
    CHAT_MESSAGE = "chat.message"
    PRIORITY_OVERRIDDEN = "priority.overridden"
    MERGE_ARMED = "merge.armed"
    MERGE_DISARMED = "merge.disarmed"
    MERGE_QUEUE = "merge.queue"
    """The merge queue gave this PR its turn; wakes its merge job."""
    MEMORY_CHANGED = "memory.changed"
    CODE_CHANGED = "code.changed"
    ACTION_EXECUTED = "action.executed"
    ACTION_FAILED = "action.failed"
    PROPOSAL_CREATED = "proposal.created"
    PROPOSAL_DECIDED = "proposal.decided"
    QUESTION_ASKED = "question.asked"
    QUESTION_ANSWERED = "question.answered"
    JOB_TIMEOUT = "job.timeout"
    JOB_PROGRESS = "job.progress"
    """A job's status or steps changed. Only the dashboard's event stream sees it
    (``EventBus.notify``); no playbook runs on it."""


class Event(BaseModel):
    """Something that happened. ``repo`` and ``number`` say what it is about."""

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    repo: str | None = None
    number: int | None = None
    sha: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "service"
    id: str = Field(default_factory=lambda: new_id("evt"))
    created_at: datetime = Field(default_factory=utc_now)


class SlashCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    args: list[str] = Field(default_factory=list)


KNOWN_COMMANDS = {"help", "review", "rerun", "merge", "label", "why", "pause"}


def parse_commands(body: str, prefix: str = "/am") -> list[SlashCommand]:
    """Return the slash commands in a comment, one per line that starts with ``prefix``.

    Lines inside fenced code blocks and quoted lines are ignored, so quoting a
    command does not run it.
    """
    commands = []
    in_fence = False
    pattern = re.compile(rf"^{re.escape(prefix)}(?:\s+(.*))?$")
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(">"):
            continue
        m = pattern.match(line)
        if not m:
            continue
        words = (m.group(1) or "help").split()
        name, args = words[0].lower(), words[1:]
        if name == "merge" and args[:1] == ["cancel"]:
            name, args = "merge_cancel", args[1:]
        if name in KNOWN_COMMANDS or name == "merge_cancel":
            commands.append(SlashCommand(name=name, args=args))
    return commands
