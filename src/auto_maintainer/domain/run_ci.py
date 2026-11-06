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

"""What "Run CI" does for a PR at its head commit: the one step that gets CI going.

A repository whose full CI starts on a label (the ``ci_gate`` role) gets that label
first. After that, or in a repository without a gate, it is the failed runs at the
head that run again. CI that is running or has passed needs nothing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .snapshot import CheckConclusion, CheckStatus, PrSnapshot

FAILED = {
    CheckConclusion.FAILURE,
    CheckConclusion.CANCELLED,
    CheckConclusion.TIMED_OUT,
    CheckConclusion.STARTUP_FAILURE,
}


class RerunTarget(BaseModel):
    run_id: int
    attempt: int | None = None
    workflow: str | None = None


class RunCiPlan(BaseModel):
    state: Literal["gated", "failed", "running", "passed", "none"]
    """``gated``: the gate label is missing. ``failed``: some runs failed and are done.
    ``running``: checks are still going. ``passed``: every check ended well. ``none``:
    no check ran at this commit."""
    label: str | None = None
    """The gate label to add, for ``gated``."""
    reruns: list[RerunTarget] = Field(default_factory=list)
    """The failed runs to run again, for ``failed``."""

    @property
    def can_run(self) -> bool:
        return self.state in ("gated", "failed")


def plan_run_ci(pr: PrSnapshot, gate_label: str | None) -> RunCiPlan:
    """What "Run CI" would do for ``pr`` now; ``gate_label`` is the label of the
    profile's ``ci_gate`` role, if it has one."""
    if gate_label and gate_label not in pr.labels:
        return RunCiPlan(state="gated", label=gate_label)
    by_run: dict[int, list] = {}
    for check in pr.checks:
        if check.run_id is not None:
            by_run.setdefault(check.run_id, []).append(check)
    reruns = []
    for run_id, checks in sorted(by_run.items()):
        done = all(c.status == CheckStatus.COMPLETED for c in checks)
        if done and any(c.conclusion in FAILED for c in checks):
            first = checks[0]
            reruns.append(
                RerunTarget(
                    run_id=run_id,
                    attempt=max((c.run_attempt or 1) for c in checks),
                    workflow=first.workflow,
                )
            )
    if reruns:
        return RunCiPlan(state="failed", reruns=reruns)
    if any(c.status != CheckStatus.COMPLETED for c in pr.checks):
        return RunCiPlan(state="running")
    if pr.checks:
        return RunCiPlan(state="passed")
    return RunCiPlan(state="none")
