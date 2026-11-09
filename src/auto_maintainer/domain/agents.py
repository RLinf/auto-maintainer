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

"""Agent tasks, the events a runtime streams, and results (design 5.7)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .ids import new_id

QUICK_TIMEOUT_SECONDS = 300
"""The longest a ``quick`` task may run."""

REPO_SKILL_FOLDERS = (
    ".agents/skills",
    ".claude/skills",
    ".codex/skills",
    ".gemini/skills",
    ".opencode/skills",
)
"""Where a repository keeps skills for coding agents, relative to its root."""


class SkillInfo(BaseModel):
    """A skill a review can follow: the repository's own (``path`` to its
    ``SKILL.md``) or the service's (``path`` empty)."""

    name: str
    path: str = ""
    description: str = ""

    @property
    def reviews(self) -> bool:
        """Whether its name or folder says it reviews, which makes it the repository's
        review skill under ``review.skill: builtin:<name>``."""
        return "review" in self.name.lower() or "review" in self.path.lower()


def parse_skill(text: str, folder: str, path: str = "") -> SkillInfo:
    """A ``SKILL.md``'s name and description from its front matter, else its folder."""
    meta: dict[str, str] = {}
    if text.startswith("---"):
        for key, value in re.findall(
            r"^(name|description):\s*(.+)$", text[:2000], re.M
        ):
            meta.setdefault(key, value.strip().strip("\"'"))
    return SkillInfo(
        name=meta.get("name") or folder,
        path=path,
        description=meta.get("description", ""),
    )


class TaskKind(StrEnum):
    TRIAGE = "triage"
    REVIEW = "review"
    VERIFY = "verify"
    FOLLOWUP = "followup"
    EVALUATE = "evaluate"
    CALIBRATE = "calibrate"
    CI_DIAGNOSE = "ci_diagnose"
    CI_ROOT_CAUSE = "ci_root_cause"
    HYGIENE = "hygiene"
    ONBOARD = "onboard"
    CHAT = "chat"
    LEARN = "learn"
    SELF_IMPROVE = "self_improve"
    FIX = "fix"
    REPRODUCE = "reproduce"
    PROBE = "probe"
    GOVERNANCE = "governance"
    TRANSLATE = "translate"
    """A review translated into the dashboard's language, for reading only."""


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_flag: bool = False
    """The CLI enforces a JSON schema on the final answer."""
    read_only_mode: bool = False
    sessions: bool = False
    acp: bool = False
    streaming: bool = False
    skill_dirs: bool = False
    """The CLI can be pointed at extra skill or instruction directories."""
    bypass: bool = False
    """The CLI has a mode that skips every permission prompt and sandbox."""
    questions: bool = False
    """The CLI can hand its permission prompts and questions to the service while it
    runs, so a reviewer can answer them on the dashboard."""


PermissionMode = Literal["restricted", "auto", "bypass"]
"""How an agent run handles actions that need permission (design 5.7).

``auto`` (the default) runs the CLI in its own auto mode and sends whatever it would
ask a person to the dashboard, where a reviewer answers. ``restricted`` denies those
requests without asking anyone. ``bypass`` skips every permission check and sandbox,
and only runs when the route, the operator, and the input's trust all allow it."""


class AgentTask(BaseModel):
    """A request for judgment."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: TaskKind
    prompt: str
    workdir: Path
    mode: Literal["read_only", "workspace_write"] = "read_only"
    permissions: PermissionMode = "auto"
    """The router sets this for every attempt from the route and the runtime."""
    question_timeout_seconds: int = 1800
    """How long a question waits for a reviewer before it counts as denied."""
    untrusted_input: bool = True
    """Whether the prompt carries text from people outside the repository's team."""
    output_schema: dict[str, Any] | None = None
    """JSON schema of the structured result, from the playbook's Pydantic model."""
    skill_dirs: list[Path] = Field(default_factory=list)
    extra_dirs: list[Path] = Field(default_factory=list)
    resume_session: str | None = None
    model: str | None = None
    effort: str | None = None
    budget: Literal["normal", "quick"] = "normal"
    """``quick`` caps the run whatever its route and agent say: low effort and
    ``QUICK_TIMEOUT_SECONDS``, for checks that must cost far less than a review."""
    timeout_seconds: int = 1800
    min_timeout_seconds: int | None = None
    """The least time the task needs, over its route's timeout: the time left in the
    test machine session the agent works in, and some to report."""
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment for the subprocess, on top of the sandbox allowlist."""
    repo: str | None = None
    pr: int | None = None
    """The PR the task is about, shown to reviewers next to the agent's questions."""
    job_id: str | None = None
    requested_by: str | None = None
    """The maintainer who asked for this work; the agent's questions go to them first."""
    lab_session: str | None = None
    """The token of a reproduction session on a test machine: the run gets the
    ``lab`` command and the lab socket (``env`` names it as ``AM_LAB_SOCKET``), and
    nothing else does (design 5.10)."""
    messages: bool = False
    """Reviewers may talk to the agent while it runs (``AgentRunner.tell`` with
    ``job_id``): set for the agent writing a review or a follow-up."""
    id: str = Field(default_factory=lambda: new_id("run"))


class QuestionKind(StrEnum):
    PERMISSION = "permission"
    """The agent wants to run a command, edit files, or use a tool."""
    INPUT = "input"
    """The agent asks one or more questions and waits for answers."""


class QuestionState(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    """No reviewer answered in time; the request was denied."""
    CANCELLED = "cancelled"
    """The run ended before anyone answered."""


class QuestionOption(BaseModel):
    """One answer a reviewer can pick for a permission request."""

    id: str
    label: str
    kind: Literal["allow", "allow_session", "deny", "cancel"]


class InputQuestion(BaseModel):
    """One question in an input request."""

    id: str
    question: str
    header: str = ""
    options: list[str] = Field(default_factory=list)
    free_text: bool = True


class AgentAnswer(BaseModel):
    """How a reviewer, or the service on its own, answered a question."""

    allow: bool
    option_id: str | None = None
    answers: dict[str, list[str]] = Field(default_factory=dict)
    """For input questions: answers keyed by question id."""
    message: str = ""
    """Sent back to the agent, for example the reason for a denial."""
    by: str = ""
    """The reviewer's login, or ``service`` for automatic answers."""
    automatic: bool = False
    at: datetime | None = None


class AgentQuestion(BaseModel):
    """Something an agent asked while it ran, waiting for a reviewer."""

    id: str = Field(default_factory=lambda: new_id("q"))
    kind: QuestionKind
    title: str
    detail: str = ""
    """What exactly the agent wants: the command, the files, or the tool input."""
    tool: str = ""
    paths: list[str] = Field(default_factory=list)
    options: list[QuestionOption] = Field(default_factory=list)
    inputs: list[InputQuestion] = Field(default_factory=list)
    runtime: str = ""
    run_id: str = ""
    task_kind: str = ""
    repo: str | None = None
    pr: int | None = None
    job_id: str | None = None
    asked_for: str | None = None
    """The maintainer who requested the work, whom the question is for; anyone who may
    answer for the repository still can."""
    untrusted_input: bool = True
    state: QuestionState = QuestionState.PENDING
    created_at: datetime | None = None
    expires_at: datetime | None = None
    answer: AgentAnswer | None = None
    waiter: str = ""
    """``host:pid`` of the process whose agent waits for the answer."""


def denial(message: str, by: str = "service") -> AgentAnswer:
    """An automatic refusal with the reason the agent is told."""
    return AgentAnswer(allow=False, message=message, by=by, automatic=True)


class AgentEventType(StrEnum):
    STARTED = "started"
    """Reported by the router when an attempt begins; adapters never emit it."""
    MESSAGE = "message"
    THINKING = "thinking"
    """The agent's reasoning as the CLI reports it, for people watching; never parsed."""
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    PERMISSION_REQUEST = "permission_request"
    ERROR = "error"
    SESSION = "session"
    """The CLI's own session id (``data["session"]``), as soon as the run has one:
    a job the service restarted resumes its agent from it."""
    FINAL = "final"


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class AgentResult(BaseModel):
    runtime: str
    ok: bool
    text: str = ""
    """The final message."""
    structured: dict[str, Any] | None = None
    session: str | None = None
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    error_kind: (
        Literal["auth", "quota", "crash", "timeout", "invalid_output", "other"] | None
    ) = None
    transcript_path: str | None = None
    duration_seconds: float = 0.0


class AgentEvent(BaseModel):
    type: AgentEventType
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    result: AgentResult | None = None
    """Set on the ``final`` event only."""


class ModelChoice(BaseModel):
    id: str
    name: str = ""


class AgentChoices(BaseModel):
    """What an agent CLI offers: its models and reasoning levels, and what it uses
    when the service names none. ``cli`` means the CLI reported them itself (ACP's
    session options); ``known`` means the adapter lists what that CLI accepts."""

    models: list[ModelChoice] = Field(default_factory=list)
    """Empty when the CLI cannot list them; any model name it accepts still works."""
    default_model: str | None = None
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None
    source: Literal["cli", "known"] = "known"


class QuotaBucket(BaseModel):
    """One limit of an agent CLI's account, as the CLI's own ``/usage`` reports it."""

    group: str = ""
    """What shares the limit, such as a family of models."""
    name: str
    """The limit, such as a weekly or a five-hour one."""
    remaining: float | None = None
    """The fraction left, from 0 to 1; ``None`` when the CLI does not say."""
    resets_at: datetime | None = None
    resets: str = ""
    """When it resets, in the CLI's own words, when it gives no exact time."""


class InstalledAgent(BaseModel):
    """An agent CLI the service installed on its host."""

    name: str
    version: str = ""
    path: str
    source: str
    """Where it came from, such as ``npm @openai/codex``."""


class AgentInstall(BaseModel):
    """An install of an agent CLI that a maintainer of the service asked for."""

    name: str
    by: str
    state: Literal["running", "done", "failed"] = "running"
    source: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    version: str = ""
    error: str = ""
    lines: list[str] = Field(default_factory=list)
    """The installer's last lines of output, for watching it."""


class AgentQuota(BaseModel):
    """What an agent CLI says is left of its account's limits."""

    runtime: str
    read_at: datetime
    buckets: list[QuotaBucket] = Field(default_factory=list)
    note: str = ""
    """Why nothing was read, or what the CLI adds about its limits."""


class RuntimeHealth(BaseModel):
    name: str
    available: bool
    version: str | None = None
    authenticated: bool | None = None
    structured_output: bool | None = None
    latency_seconds: float | None = None
    detail: str = ""
    choices: AgentChoices | None = None
    """Its models and reasoning levels, when it is installed."""
