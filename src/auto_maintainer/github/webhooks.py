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

"""Webhook verification and routing hints.

A delivery is never trusted as state. It only tells the poller which repository
and PR or issue to read again.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

_PR_EVENTS = {
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
}
_ISSUE_EVENTS = {"issues", "issue_comment"}
_CHECK_EVENTS = {"check_run", "check_suite", "workflow_run"}


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check an ``X-Hub-Signature-256`` header against the delivery body.

    Args:
        secret: The webhook secret.
        body: The raw request body.
        header: The header value, ``sha256=<hex>``.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def hint(event_name: str, payload: dict[str, Any]) -> tuple[str, int | None] | None:
    """The repository and PR or issue number a delivery is about.

    Returns:
        ``(repo, number)``, with ``number`` ``None`` when the delivery is about the
        repository as a whole; ``None`` when it names no repository.
    """
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None
    number: int | None = None
    if event_name in _PR_EVENTS:
        number = (payload.get("pull_request") or {}).get("number")
    elif event_name in _ISSUE_EVENTS:
        number = (payload.get("issue") or {}).get("number")
    elif event_name in _CHECK_EVENTS:
        obj = payload.get(event_name) or {}
        pulls = obj.get("pull_requests") or []
        if pulls:
            number = pulls[0].get("number")
    return repo, int(number) if number is not None else None
