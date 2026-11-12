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


"""A PR's CI at its head commit as the service reads it, and what "Run CI" does.

A head often has several runs of one workflow: a push and a label each start one,
a rerun adds an attempt, and a concurrency group cancels the run a newer one
replaced. Only the newest run of each workflow counts (``latest_checks``), as it
does for the forge's own required checks. A repository's gate jobs (``ci.start``:
they fail on purpose when CI was not asked for) and summary jobs (they only report
the others) never count as failures of their own (``ci_view``).

"Run CI" is the one step that gets CI going: the gate label when the repository
starts its full CI on one and the PR lacks it, approving the workflows the forge
holds for a maintainer, or rerunning the runs that failed. A run a gate job stopped
is never rerun: a rerun replays the event it started from, labels and all.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .profile import CiStart
from .snapshot import CheckConclusion, CheckRun, CheckStatus, PrSnapshot

FAILED = {
    CheckConclusion.FAILURE,
    CheckConclusion.CANCELLED,
    CheckConclusion.TIMED_OUT,
    CheckConclusion.STARTUP_FAILURE,
}


def latest_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """``checks`` without the ones a newer run superseded: for each workflow, only
    its newest run (the highest run id), and in it the newest check of each name (a
    rerun's attempt); a check outside any workflow run (a commit status, another
    app's check), the newest of each name."""
    newest_run: dict[str, int] = {}
    for c in checks:
        if c.workflow and c.run_id is not None:
            newest_run[c.workflow] = max(newest_run.get(c.workflow, c.run_id), c.run_id)
    kept: dict[tuple[str, str], CheckRun] = {}
    for c in checks:
        if c.workflow and c.run_id is not None and c.run_id != newest_run[c.workflow]:
            continue
        before = kept.get(_key(c))
        if before is None or (c.run_attempt or 0, c.id) > (
            before.run_attempt or 0,
            before.id,
        ):
            kept[_key(c)] = c
    return [c for c in checks if kept.get(_key(c)) is c]


def _key(c: CheckRun) -> tuple[str, str]:
    if c.workflow and c.run_id is not None:
        return (f"run:{c.run_id}", c.name)
    return (f"other:{c.workflow or ''}", c.name)


class CiView(BaseModel):
    """A PR's CI at its head, from its newest runs."""

    state: Literal[
        "none", "gated", "stopped", "approval", "running", "failed", "passed"
    ]
    """``none``: nothing ran. ``gated``: the gate label is missing. ``stopped``: the
    newest run stopped at a gate job although the label is there now, and no run
    started since. ``approval``: the forge holds workflows for a maintainer.
    ``running``: checks are still going. ``failed``: checks failed (``failed``).
    ``passed``: every check ended well."""
    failed: list[CheckRun] = Field(default_factory=list)
    """The failures to look into, whatever the state: never a gate or summary job,
    unless a summary job failed with nothing else to show for it."""
    cancelled: list[CheckRun] = Field(default_factory=list)
    """Checks of the newest runs that were cancelled."""
    running: int = 0
    passed: int = 0
    label: str | None = None
    held: list[CheckRun] = Field(default_factory=list)
    """Checks the forge holds until a maintainer approves running them."""


def ci_view(
    pr: PrSnapshot, gate_label: str | None, start: CiStart | None = None
) -> CiView:
    """``pr``'s CI as the repository runs it: ``gate_label`` is the label of the
    profile's ``ci_gate`` role, ``start`` its ``ci.start``."""
    start = start or CiStart()
    gates = {j.lower() for j in start.gate_jobs}
    summaries = {j.lower() for j in start.summary_jobs}
    optional = {j.lower() for j in start.optional_checks}
    checks = latest_checks(pr.checks)

    def named(c: CheckRun, names: set[str]) -> bool:
        # A job of a reusable workflow reads ``caller / job``.
        return c.name.lower() in names or c.name.lower().split(" / ")[-1] in names

    held = [c for c in checks if c.awaiting_approval]
    done = [c for c in checks if c.status == CheckStatus.COMPLETED]
    failed = [c for c in done if c.failed]
    cancelled = [c for c in done if c.conclusion == CheckConclusion.CANCELLED]
    real = [
        c
        for c in failed
        if not named(c, gates) and not named(c, summaries) and not named(c, optional)
    ]
    stopped = [c for c in failed if named(c, gates)]
    running = sum(1 for c in checks if c.status != CheckStatus.COMPLETED)
    passed = sum(
        1
        for c in done
        if c.conclusion
        in (CheckConclusion.SUCCESS, CheckConclusion.NEUTRAL, CheckConclusion.SKIPPED)
    )
    view: dict = {
        "running": running,
        "passed": passed,
        "held": held,
        "cancelled": cancelled,
    }
    if gate_label and gate_label not in pr.labels:
        # What ran without the label (a lint workflow) still shows what failed.
        return CiView(state="gated", label=gate_label, failed=real, **view)
    if held:
        return CiView(state="approval", failed=real, **view)
    if running:
        return CiView(state="running", failed=real, **view)
    if real:
        return CiView(state="failed", failed=real, **view)
    if stopped:
        return CiView(state="stopped", **view)
    summary_failed = [c for c in failed if named(c, summaries)]
    if summary_failed or cancelled:
        return CiView(state="failed", failed=summary_failed, **view)
    if checks:
        return CiView(state="passed", **view)
    return CiView(state="none", **view)


class RerunTarget(BaseModel):
    run_id: int
    attempt: int | None = None
    workflow: str | None = None


class RunCiPlan(BaseModel):
    state: Literal["gated", "failed", "approval", "running", "passed", "none"]
    """``gated``: the gate label is missing. ``failed``: some runs failed and are done.
    ``approval``: GitHub holds a workflow until a maintainer approves running it.
    ``running``: checks are still going. ``passed``: every check ended well. ``none``:
    no check ran at this commit, or the newest run stopped at a gate job before the
    label came (only a new run, from a push or the label, starts it)."""
    waiting: list[str] = Field(default_factory=list)
    """The workflows waiting for approval, for ``approval``."""
    approve_url: str | None = None
    """Where on GitHub a maintainer approves them, for ``approval``."""
    approve_runs: list[int] = Field(default_factory=list)
    """The workflow runs to approve, for ``approval``."""
    label: str | None = None
    """The gate label to add, for ``gated``."""
    reruns: list[RerunTarget] = Field(default_factory=list)
    """The failed runs to run again, for ``failed``."""

    @property
    def can_run(self) -> bool:
        return self.state in ("gated", "failed", "approval")


def rerun_targets(checks: list[CheckRun]) -> list[RerunTarget]:
    """The runs of ``checks`` to run again: each finished run with a failed or
    cancelled check, at its newest attempt."""
    by_run: dict[int, list[CheckRun]] = {}
    for check in checks:
        if check.run_id is not None:
            by_run.setdefault(check.run_id, []).append(check)
    out = []
    for run_id, group in sorted(by_run.items()):
        done = all(c.status == CheckStatus.COMPLETED for c in group)
        if done and any(c.conclusion in FAILED for c in group):
            out.append(
                RerunTarget(
                    run_id=run_id,
                    attempt=max((c.run_attempt or 1) for c in group),
                    workflow=group[0].workflow,
                )
            )
    return out


def plan_run_ci(
    pr: PrSnapshot, gate_label: str | None, start: CiStart | None = None
) -> RunCiPlan:
    """What "Run CI" would do for ``pr`` now; ``gate_label`` is the label of the
    profile's ``ci_gate`` role, if it has one, and ``start`` its ``ci.start``."""
    view = ci_view(pr, gate_label, start)
    if view.state == "gated":
        return RunCiPlan(state="gated", label=gate_label)
    gates = {j.lower() for j in (start.gate_jobs if start else [])}
    latest = latest_checks(pr.checks)
    stopped_runs = {
        c.run_id
        for c in latest
        if c.failed and c.name.lower().split(" / ")[-1] in gates
    }
    reruns = [r for r in rerun_targets(latest) if r.run_id not in stopped_runs]
    if reruns:
        return RunCiPlan(state="failed", reruns=reruns)
    if view.state == "approval":
        return RunCiPlan(
            state="approval",
            waiting=sorted({c.workflow or c.name for c in view.held}),
            approve_url=next((c.details_url for c in view.held if c.details_url), None)
            or f"{pr.url}/checks",
            approve_runs=sorted({c.run_id for c in view.held if c.run_id}),
        )
    if view.state == "running":
        return RunCiPlan(state="running")
    if view.state == "passed":
        return RunCiPlan(state="passed")
    return RunCiPlan(state="none")
