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

"""What an agent did while it worked on a job, as steps the dashboard shows live.

Steps are internal: they carry the agent's own words and the commands it ran, so only
people who may view the job's repository see them. Text is cut short and anything that
looks like a credential is removed before a step is kept.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .agents import AgentEvent, AgentEventType
from .clock import utc_now
from .secrets import redact_secrets

StepKind = Literal[
    "start", "message", "thinking", "tool", "result", "question", "error", "done"
]

_INPUT_KEYS = (
    "command",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)


class JobStep(BaseModel):
    """One thing an agent did. The dashboard words ``start`` and ``done`` itself."""

    kind: StepKind
    text: str = ""
    detail: str = ""
    runtime: str = ""
    task_kind: str = ""
    ok: bool | None = None
    """For ``done`` and ``result``: whether the run or the tool call succeeded."""
    at: datetime = Field(default_factory=utc_now)


def _clean(text: str, limit: int) -> str:
    text, _ = redact_secrets(text)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _input_summary(data: dict[str, Any]) -> str:
    value = data.get("input")
    if isinstance(value, dict):
        for key in _INPUT_KEYS:
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value
    return ""


def step_from_event(event: AgentEvent, runtime: str, task_kind: str) -> JobStep | None:
    """The step ``event`` adds to a job's progress, or ``None`` for events that add
    nothing a person would read (usage counters)."""
    base = {"runtime": runtime, "task_kind": task_kind}
    kind = event.type
    if kind == AgentEventType.STARTED:
        return JobStep(kind="start", **base)
    if kind in (AgentEventType.MESSAGE, AgentEventType.THINKING):
        text, _ = redact_secrets(event.text)
        step = "message" if kind == AgentEventType.MESSAGE else "thinking"
        return JobStep(kind=step, text=text, **base) if text.strip() else None
    if kind == AgentEventType.TOOL_CALL:
        return JobStep(
            kind="tool",
            text=_clean(event.text, 200),
            detail=_clean(_input_summary(event.data), 600),
            **base,
        )
    if kind == AgentEventType.TOOL_RESULT:
        failed = event.data.get("is_error") or (
            isinstance(event.data.get("exit_code"), int)
            and event.data["exit_code"] != 0
        )
        output = event.text or str(event.data.get("output") or "")
        return JobStep(kind="result", detail=_clean(output, 600), ok=not failed, **base)
    if kind == AgentEventType.PERMISSION_REQUEST:
        return JobStep(
            kind="question",
            text=_clean(event.text, 300),
            ok=event.data.get("allow"),
            detail="automatic" if event.data.get("automatic") else "",
            **base,
        )
    if kind == AgentEventType.ERROR:
        return JobStep(kind="error", text=_clean(event.text, 1000), **base)
    if kind == AgentEventType.FINAL:
        result = event.result
        ok = bool(result and result.ok)
        error = (result.error or "") if result else ""
        return JobStep(kind="done", ok=ok, text=_clean(error, 1000), **base)
    return None
