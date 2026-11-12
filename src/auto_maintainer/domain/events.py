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
    COMMENT_CHECK = "comments.check"
    """Check a PR's open review comments against its code (``thread_ids``: only
    those, checked again even at the same head)."""
    REVIEW_REPRODUCE = "review.reproduce"
    """No longer sent: reviews test in their own session. Kept so jobs and events
    stored before that still load (the reproduce playbook ends them)."""
    CHAT_MESSAGE = "chat.message"
    PRIORITY_OVERRIDDEN = "priority.overridden"
    """A review asked for its PR to be reproduced on a test machine; the reproduce
    playbook runs it and adds the result to the review."""
    PRIORITY_RESCORE = "priority.rescore"
    """The rubric's dimensions changed since this PR was scored; the evaluate
    playbook scores every dimension again."""
    QUALITY_REQUESTED = "quality.requested"
    """Score the quality of a PR's latest review again, or for the first time when it
    has none; the quality playbook runs it."""
    QUICK_QUALITY = "quality.quick"
    """Give a PR nobody reviewed yet a quick quality score from its diff, in the
    background; the quality scan asks for it."""
    QUICK_LEARN = "quality.learn"
    """Draw again the rules a repository's quick quality scores follow, from the quick
    scores its reviews corrected."""
    MERGE_ARMED = "merge.armed"
    MERGE_DISARMED = "merge.disarmed"
    MERGE_QUEUE = "merge.queue"
    """The merge queue gave this PR its turn; wakes its merge job."""
    SCOPE_NEEDED = "forge.scope_needed"
    """The forge refused a write for a scope the service's sign-in lacks; the
    service asks its owner to add it (``Service.request_scopes``)."""
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
    PR_REFRESHED = "pr.refreshed"
    """A PR's snapshot was read again after the service wrote to it, whether or not
    anything that starts a playbook changed. Only dashboards see it."""
    SERVICE_CHANGED = "service.changed"
    """Something on the service's own pages changed: an agent was installed or signed
    in or out, or the sign-in sweep found other agents usable. Only dashboards see
    it."""


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
