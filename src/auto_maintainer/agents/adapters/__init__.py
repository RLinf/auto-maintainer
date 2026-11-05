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

"""One adapter per agent CLI, each an ``ExecRuntime`` subclass."""

from __future__ import annotations

import json
from typing import Any

from ...domain.agents import AgentEvent, AgentEventType


def loads_object(line: str) -> dict[str, Any] | None:
    """Parse one output line as a JSON object, or return ``None``."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def partial(**data: Any) -> AgentEvent:
    """A partial update to the run's result (see ``agents.exec``)."""
    return AgentEvent(
        type=AgentEventType.FINAL, data={k: v for k, v in data.items() if v is not None}
    )


def short(value: Any, limit: int = 2000) -> str:
    """A string form of ``value`` cut to ``limit`` characters."""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return text if len(text) <= limit else text[:limit] + "…"
