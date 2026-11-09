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

"""GitLab webhooks (design 5.3, 5.16): a delivery carries the shared secret in
``X-Gitlab-Token`` (no signature of the body), and names its project by path."""

from __future__ import annotations

import hmac
from typing import Any


def verify_token(secret: bytes, body: bytes, header: str | None) -> bool:
    """Whether ``X-Gitlab-Token`` is the webhook's secret."""
    return bool(secret and header) and hmac.compare_digest(
        secret, (header or "").encode()
    )


def hint(event_name: str, payload: dict[str, Any]) -> tuple[str, int | None] | None:
    """The project (its path) and merge request a delivery is about; ``None`` when it
    names no project."""
    project = (payload.get("project") or {}).get("path_with_namespace")
    if not project:
        return None
    kind = payload.get("object_kind")
    number: int | None = None
    if kind == "merge_request":
        number = (payload.get("object_attributes") or {}).get("iid")
    elif kind in ("note", "pipeline"):
        number = (payload.get("merge_request") or {}).get("iid")
    return project, int(number) if number else None
