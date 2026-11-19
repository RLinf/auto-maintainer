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

"""What starts a review (design 5.5, ``review.trigger``).

A review request comes from a reviewer (the dashboard, ``/am review``, chat) or from
the periodic scan. A reviewer's request produces drafts for that reviewer to edit and
post. With ``review.trigger: events``, PR events start reviews without a request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ..domain.actions import ActionKind, ActionStatus
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.ports import Store
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord
from ..domain.review import ReviewState

AUTO_EVENTS = (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED, EventKind.PR_RECONCILED)
FOLLOWUP_STATES = (ReviewState.FOLLOWUP_REVIEW, ReviewState.APPROVED_NEW_COMMITS)
SETTLE_SECONDS = 600
"""How long a review that a push starts on its own (``review.trigger: events``)
waits for more pushes before it begins: on 2026-11-19 five pushes to one PR in 35
minutes each started a full review and a session on a test machine."""
SUPERSEDED_ON_PUSH = ("review", "followup", "reproduce")
"""Jobs a newer push stops when nobody asked for them and they work on an older
commit (``engine.Engine.handle_event``)."""
RECORDED = (
    ActionStatus.SHADOWED,
    ActionStatus.PROPOSED,
    ActionStatus.QUEUED,
    ActionStatus.INTENT,
    ActionStatus.EXECUTED,
    ActionStatus.SUPERSEDED,
)
"""States of a review draft that count as the service having reviewed a commit,
posted or not; a rejected or failed one does not."""


@dataclass(frozen=True)
class ReviewRequest:
    source: str
    """``dashboard``, ``comment``, ``chat``, or ``scan``."""
    by: str | None = None
    """The reviewer who asked; their drafts become proposals for them. ``None`` for scans."""
    agent: str | None = None
    """The runtime the reviewer chose; ``None`` follows the route."""
    own: bool = False
    """A review of the PR's own by one of its owners or reviewers, beside the reviews
    others asked for (``web/api.py:_request_review``): it runs as a new review
    whatever the PR's review state, and the follow-up leaves it alone."""
    full: bool = False
    """A review of the whole PR asked for by name (the dashboard's Full review,
    ``/am review full``, chat), whatever reviews it already has: it runs as a new
    review, and the follow-up leaves it alone."""

    @property
    def manual(self) -> bool:
        return self.by is not None


def review_request(event: Event) -> ReviewRequest | None:
    """The review request carried by ``event``, if any."""
    if (
        event.kind == EventKind.DASHBOARD_COMMAND
        and event.payload.get("command") == "review"
    ):
        source = str(event.payload.get("source") or event.source or "dashboard")
        if source == "scan":
            return ReviewRequest(source="scan")
        by = event.payload.get("by")
        agent = event.payload.get("agent")
        return (
            ReviewRequest(
                source=source,
                by=str(by),
                agent=str(agent) if agent else None,
                own=bool(event.payload.get("own")),
                full=bool(event.payload.get("full")),
            )
            if by
            else None
        )
    if event.kind == EventKind.PR_COMMENT:
        asked = [
            c for c in event.payload.get("commands", []) if c.get("name") == "review"
        ]
        author = event.payload.get("author")
        if not asked or not author:
            return None
        full = any(str(a).lower() == "full" for c in asked for a in c.get("args") or [])
        return ReviewRequest(source="comment", by=str(author), full=full)
    return None


def starts_review(event: Event, profile: RepoProfile | None) -> bool:
    """Whether ``event`` should start a review or follow-up job at all."""
    if event.number is None:
        return False
    if review_request(event) is not None:
        return True
    return (
        event.kind in AUTO_EVENTS
        and profile is not None
        and profile.review.trigger == "events"
    )


def review_key(name: str, event: Event) -> str:
    """One job per reviewer request; one per head SHA for scans and events."""
    request = review_request(event)
    base = f"{name}:{event.repo}#{event.number}"
    if request is not None and request.manual:
        return f"{base}:req:{event.id}"
    return f"{base}@{(event.sha or '-')[:12]}"


def automatic(event: Event) -> bool:
    """Whether a job started from ``event`` runs without anyone having asked: a
    push, a scan, or a reproduction a review nobody asked for started."""
    if event.kind == EventKind.DASHBOARD_COMMAND:
        return not event.payload.get("by")
    return event.kind != EventKind.PR_COMMENT


def replaced(event: Event, head_sha: str) -> bool:
    """Whether a newer push replaced the commit a push-started job was for."""
    return (
        event.kind in (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED)
        and bool(event.sha)
        and not same_commit(event.sha, head_sha)
    )


def settle_delay(event: Event, since: datetime, now: datetime) -> int | None:
    """Seconds an automatic review started by a push still waits for pushes to
    settle (``SETTLE_SECONDS``, counted from ``since``, when its job was made), or
    ``None`` when it may begin. A newly opened PR is reviewed at once."""
    if event.kind != EventKind.PR_SYNCHRONIZED:
        return None
    if review_request(event) is not None:
        return None
    waited = (now - since).total_seconds()
    return math.ceil(SETTLE_SECONDS - waited) if waited < SETTLE_SECONDS else None


async def recorded_review(store: Store, repo: str, number: int) -> ActionRecord | None:
    """The newest review or follow-up the service wrote of PR ``number``, posted or
    only recorded (a scan's or a push's, in shadow): automatic reviews count it, so a
    PR whose reviews nobody posts gets a follow-up at its next commit, not another
    full review."""
    for record in await store.list_actions(
        repo=repo,
        number=number,
        kinds=[ActionKind.REVIEW_POST],
        statuses=list(RECORDED),
        limit=20,
    ):
        if record.action.head_sha:
            return record
    return None
