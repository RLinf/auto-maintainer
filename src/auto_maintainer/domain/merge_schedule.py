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


"""Merge scheduling (design 5.5): which scheduled PRs act now, and what each is doing.

A PR is schedulable once it meets its repository's target for running its final CI
and merging (``merge.schedule.ready``: by default every condition the merge rules
hold a PR to, such as its approvals and resolved conversations), whether or not it
is up to date: the schedule merges the base in before its CI. A scheduled PR is an
armed one (``MergeArm``), placed in the repository's order by its ``position``.
What the target leaves out but a merge needs is waited for after CI, without a turn
(the ``approval`` phase).

When PRs must be up to date with the base before they merge, a merge puts every
other PR behind again, so scheduled PRs take turns (``serial``): one merges the
base, runs its CI, and merges, then the next. Otherwise several run their CI at once
(``merge.schedule.parallel``). A PR that is doing something (``ACTIVE``) keeps its
turn, so its CI is not wasted; the next turn goes down the order, skipping PRs in
the failure lane, waiting after CI, or held back by something else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .merge_gate import GateItem, GateResult, MergeArm
from .profile import READY_CONDITIONS, RepoProfile
from .snapshot import PrSnapshot, RepoInfo

Phase = Literal[
    "queued",
    "updating",
    "resolving",
    "starting",
    "running",
    "rerunning",
    "diagnosing",
    "approval",
    "merging",
    "blocked",
    "failed",
    "held",
    "merged",
]
ACTIVE: frozenset[str] = frozenset(
    {
        "updating",
        "resolving",
        "starting",
        "running",
        "rerunning",
        "diagnosing",
        "merging",
    }
)
"""Phases in which a PR keeps its turn."""
WAITING: frozenset[str] = frozenset({"queued"})
"""The phase of a PR that may take a turn when one is free."""

LANDING_SECONDS = 600
"""How long a requested base merge or rerun may take to show on the forge before
the schedule takes it to have not happened."""

UPDATES_PER_TURN = 3
"""How often a PR merges the base during one turn before the base is taken to move
too fast for it (people merging by hand while it runs)."""


def state_key(repo: str, number: int) -> str:
    """Where the store keeps a scheduled PR's ``ScheduleState``."""
    return f"merge-schedule:{repo}#{number}"


RESOLVE_SECONDS = 3600
"""How long the schedule's resolution of a PR's conflict may take, from starting the
agent to its push showing on the forge, before the schedule takes it to have failed."""


def resolve_key(repo: str, number: int) -> str:
    """Where the store keeps how the schedule's resolution of a PR's conflicts went:
    ``sha`` (the head it resolved), ``outcome`` (``started``, ``proposed``, or
    ``refused``), ``why``, ``action_id``, ``at``."""
    return f"merge-schedule-resolve:{repo}#{number}"


def skip_key(repo: str, number: int) -> str:
    """The head a maintainer unscheduled, which automatic scheduling leaves alone."""
    return f"merge-schedule-skip:{repo}#{number}"


class ScheduleState(BaseModel):
    """What a scheduled PR is doing, kept by its merge job."""

    phase: Phase = "queued"
    since: datetime | None = None
    """When it entered ``phase``."""
    sha: str | None = None
    """The head the phase is about."""
    detail: str = ""
    """One line on what it waits for, or why it failed or is held."""
    updates: int = 0
    """Base merges during this turn (``UPDATES_PER_TURN``)."""
    updated_from: str | None = None
    """The head a base merge the scheduler asked for started from."""
    started_at: datetime | None = None
    """When the step that should start its CI at ``sha`` was taken, or CI was first
    seen waiting to start."""
    running_since: datetime | None = None
    looked_into: list[str] = Field(default_factory=list)
    """``<sha>:<why>`` for each stall an agent looked into, so it looks once."""
    finding: str = ""
    """What that agent found."""
    merge_attempts: int = 0
    """Merges the forge refused at this head: each new attempt is an action of its
    own (``Action.idempotency_key``)."""
    blocked_on: str = ""
    """For ``blocked``: why the forge refused its merge or base merge;
    ``workflow_scope`` when the service's sign-in lacks the workflow scope, which
    only a new sign-in, read at the next start, can change."""
    failed_on_ci: bool = False
    """It went to the failure lane because its CI failed (not a refused merge or
    base merge, nor a conflict): CI that runs again at the same head brings it
    back."""
    failed_jobs: list[str] = Field(default_factory=list)
    causes: list[str] = Field(default_factory=list)
    """The failure lane's root causes, as the PR author reads them."""
    approval_needed: bool = False
    """A fork's workflows wait for a person to approve them."""
    retries: int = 0
    """Times a maintainer sent it back from the failure lane at this head."""
    retry: bool = False
    """Sent back and not yet run again: its turn reruns the runs that failed, past
    the rerun budget, since a maintainer asked."""
    rerun_of: list[int] = Field(default_factory=list)
    """The failed checks a rerun was asked for: until newer attempts replace them,
    the rerun has not started, and they are not a failure to act on again."""
    not_before: datetime | None = None
    """The forge's rate limit refused a step: it is taken again only from then."""
    transient: int = 0
    """Steps the forge's rate limit refused at this head: each new attempt is an
    action of its own (``Action.idempotency_key``)."""
    queued: bool = False
    """Its merge went to the forge's merge queue, which merges it in its own time."""
    blocked_rule: str = ""
    """The forge's own merge verdict (``PrSnapshot.merge_state``) held back a merge
    that the service's merge gate allowed, at this head."""


def serial(info: RepoInfo, profile: RepoProfile) -> bool:
    """Whether scheduled PRs take turns: when a PR must be current with the base."""
    return bool(info.requires_up_to_date) or profile.merge.update_branch


def position(arm: MergeArm) -> float:
    return arm.position if arm.position is not None else arm.armed_at.timestamp()


def ordered(arms: list[MergeArm]) -> list[MergeArm]:
    """``arms`` in the repository's merge order."""
    return sorted(arms, key=lambda a: (position(a), a.armed_at, a.number))


def next_position(arms: list[MergeArm], now: datetime) -> float:
    """The position of a PR scheduled now: after every other."""
    return max([now.timestamp(), *(position(a) + 1 for a in arms)])


def condition_of(item: GateItem) -> str | None:
    """The ``READY_CONDITIONS`` name of a merge gate item, or ``None`` for one that
    is never a condition to schedule (CI, being up to date, armed, a pause)."""
    if item.name.startswith("hygiene:"):
        return "hygiene"
    return item.name if item.name in READY_CONDITIONS else None


def ready_conditions(
    profile: RepoProfile, arm: MergeArm | None = None
) -> tuple[str, ...]:
    """What ``profile`` holds a PR to before it is schedulable; nothing for one a
    maintainer scheduled by hand (``MergeArm.forced``), which waits for the merge
    rules after its CI instead."""
    if arm is not None and arm.forced:
        return ()
    ready = profile.merge.schedule.ready
    return tuple(ready) if ready is not None else READY_CONDITIONS


def candidate_items(gate: GateResult, ready: tuple[str, ...]) -> list[GateItem]:
    """The merge gate's items that decide whether a PR is schedulable: those of the
    conditions in ``ready``."""
    return [i for i in gate.items if condition_of(i) in ready]


def holding_back(gate: GateResult, ready: tuple[str, ...]) -> list[GateItem]:
    """What keeps a PR from being schedulable; empty when it is."""
    return [i for i in candidate_items(gate, ready) if not i.ok]


def left_for_merge(gate: GateResult, ready: tuple[str, ...]) -> list[GateItem]:
    """What a merge still needs that ``ready`` does not hold a PR to before its CI
    (approvals, for a repository whose reviewers approve after CI): a scheduled PR
    whose CI passed waits for these without a turn."""
    return [
        i
        for i in gate.items
        if not i.ok and condition_of(i) is not None and condition_of(i) not in ready
    ]


def turns(
    order: list[int],
    states: dict[int, ScheduleState],
    one_at_a_time: bool,
    parallel: int,
) -> list[int]:
    """The scheduled PRs that may act now, in ``order``: those already ``ACTIVE``
    keep their turn, and waiting ones take the free turns down the order."""
    width = 1 if one_at_a_time else max(1, parallel)
    phase = {n: (states.get(n) or ScheduleState()).phase for n in order}
    active = [n for n in order if phase[n] in ACTIVE]
    free = [n for n in order if phase[n] in WAITING]
    return (active + free)[: max(width, len(active))]


def only_base_merged(pr: PrSnapshot, armed_sha: str, updaters: set[str]) -> bool:
    """Whether every commit on ``pr`` after ``armed_sha`` is a merge of its base made
    by one of ``updaters`` (the scheduler, the service): a branch update the
    schedule asked for, or its resolution of a conflict with the base, which keeps
    the schedule. ``pr.commits`` is oldest first."""
    shas = [c.sha for c in pr.commits]
    if armed_sha not in shas or shas[-1] == armed_sha:
        return False
    after = pr.commits[shas.index(armed_sha) + 1 :]
    who = {u.lower() for u in updaters}
    # A push made as a person names them by their no-reply address, which the forge
    # does not always map back to their login.
    emails = {f"{u}@users.noreply.github.com" for u in who}
    return all(
        (
            (c.author_login or "").lower() in who
            or (c.author_email or "").lower() in emails
        )
        and c.message.startswith(
            (
                f"Merge branch '{pr.base_ref}'",
                "Merge remote-tracking branch",
                # The schedule's own resolution of a conflict with the base
                # (``playbooks/merge_base.py``, pushed as the scheduler).
                f"Merge {pr.base_ref} into #{pr.number}",
            )
        )
        for c in after
    )
