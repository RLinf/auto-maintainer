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

from dataclasses import dataclass

from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.review import ReviewState

AUTO_EVENTS = (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED, EventKind.PR_RECONCILED)
FOLLOWUP_STATES = (ReviewState.FOLLOWUP_REVIEW, ReviewState.APPROVED_NEW_COMMITS)


@dataclass(frozen=True)
class ReviewRequest:
    source: str
    """``dashboard``, ``comment``, ``chat``, or ``scan``."""
    by: str | None = None
    """The reviewer who asked; their drafts become proposals for them. ``None`` for scans."""
    agent: str | None = None
    """The runtime the reviewer chose; ``None`` follows the route."""

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
                source=source, by=str(by), agent=str(agent) if agent else None
            )
            if by
            else None
        )
    if event.kind == EventKind.PR_COMMENT and any(
        c.get("name") == "review" for c in event.payload.get("commands", [])
    ):
        author = event.payload.get("author")
        return ReviewRequest(source="comment", by=str(author)) if author else None
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
