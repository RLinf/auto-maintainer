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

"""What went wrong in a finished agent job, read from its saved steps (design 5.11):
the input of the retrospective that turns trouble into lessons and suggestions.

A job is worth a look only when something went wrong in it: its run failed or hit an
error, a request of its agent was denied, or several of its tool calls failed with
something to say (a command on a test machine that exited non-zero with an error, a
refused one; a search that found nothing is no failure). A job that went smoothly
costs no agent call, and a repository gets at most ``PER_DAY`` looks a day.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .clock import utc_now
from .ids import new_id
from .progress import JobStep

FAILED_CALLS = 3
"""Failed tool calls that make a job worth a look on their own. One that failed
without a word (a search that found nothing) is not counted."""
PER_DAY = 20
"""Retrospectives a repository gets a day at most, whatever the trouble."""
DIGEST_CHARS = 12_000


class Trouble(BaseModel):
    """What went wrong in a job, and the timeline of its agent's work around it."""

    failed_calls: int = 0
    errors: int = 0
    denied: int = 0
    run_failed: bool = False
    machines: list[str] = Field(default_factory=list)
    """Test machines whose output the job's steps show."""
    digest: str = ""

    @property
    def worth_a_look(self) -> bool:
        return (
            self.run_failed
            or self.errors > 0
            or self.denied > 0
            or self.failed_calls >= FAILED_CALLS
        )


def trouble(steps: list[JobStep], job_error: str | None = None) -> Trouble:
    """``steps`` of one job (oldest first) as ``Trouble``: every tool call in order,
    with the output of those that failed, the errors and denials, and the agent's
    last account of its work."""
    found = Trouble(run_failed=bool(job_error))
    lines: list[str] = []
    machines: list[str] = []
    tool: JobStep | None = None
    account = ""
    for step in steps:
        if step.kind == "tool":
            tool = step
        elif step.kind == "result":
            call = f"{tool.text}: {tool.detail}" if tool else "(a tool call)"
            if step.ok is False and step.detail.strip():
                found.failed_calls += 1
                lines.append(f"FAILED {call}\n    -> {step.detail[-500:]}")
            elif step.ok is False:
                lines.append(f"no output, exit non-zero: {call[:240]}")
            else:
                lines.append(f"ok     {call[:240]}")
            tool = None
        elif step.kind == "error":
            found.errors += 1
            lines.append(f"ERROR  {step.text[:600]}")
        elif step.kind == "question" and step.ok is False:
            found.denied += 1
            lines.append(f"DENIED {step.text[:300]}")
        elif step.kind == "output" and step.runtime and step.runtime not in machines:
            machines.append(step.runtime)
        elif step.kind == "done":
            if step.ok is False:
                found.run_failed = True
                lines.append(f"RUN FAILED {step.text[:600]}")
            if step.output.strip():
                account = step.output.strip()
    if job_error:
        lines.append(f"JOB FAILED {job_error[:600]}")
    text = "\n".join(lines)
    if len(text) > DIGEST_CHARS:
        # Keep how it started and how it ended; failures are kept in full above.
        head, tail = lines[:15], []
        size = sum(len(x) for x in head)
        for line in reversed(lines[15:]):
            if size + len(line) > DIGEST_CHARS:
                break
            tail.insert(0, line)
            size += len(line)
        text = "\n".join([*head, "…", *tail])
    if account:
        text += "\n\nThe agent's own account at the end:\n" + account[-1500:]
    found.machines = machines
    found.digest = text
    return found


SUGGESTIONS_KEY = "agent-suggestions"
"""Where the store keeps the agents' suggestions to the service's owner."""
KEEP_SUGGESTIONS = 200


class Suggestion(BaseModel):
    """A change to the service itself that its agents suggest after it made their
    work fail or go worse (a rule refusing a legitimate command, a tool missing from
    their image, a misleading instruction), for the service's owner, who may start a
    self-improvement with it (design 5.12). The same problem met again counts up."""

    title: str
    problem: str
    proposal: str
    repo: str | None = None
    playbook: str = ""
    jobs: list[str] = Field(default_factory=list)
    """The jobs that ran into it, newest last."""
    count: int = 1
    first_at: datetime = Field(default_factory=utc_now)
    last_at: datetime = Field(default_factory=utc_now)
    state: Literal["open", "approved", "dismissed"] = "open"
    decided_by: str | None = None
    improvement: str | None = None
    """For an approved one: the request the self-improvement runs on (its event id)."""
    id: str = Field(default_factory=lambda: new_id("sug"))
