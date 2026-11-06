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

"""The order armed PRs merge in, for a repository whose PRs must be up to date with
the base branch before they merge (design 5.5).

Merging one PR puts every other armed PR behind the base again, and each must merge
the base and run its CI once more before it can merge. So PRs take turns: one PR (the
head) updates, waits for its CI, and merges while the others wait, and the order
spends as little CI as it can:

- The head keeps its turn while it is getting there: ready to merge, needing an
  update, or waiting only for its own CI. It loses the turn when anything else stops
  it (a failed check, a missing approval), or when it merges or is disarmed.
- Then PRs that can merge as they are (up to date, CI passed) come first, the one
  whose CI costs most first: merging it saves running that CI again, while a cheap PR
  (a documentation change) pays little to rerun after it.
- Then PRs that must merge the base first, the one whose CI costs least first: each
  pays its CI once whatever the order, so quick PRs do not wait behind slow ones.
- PRs that cannot merge yet for other reasons come last, by arm time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .merge_gate import GateResult
from .snapshot import PrSnapshot

CI_ITEM_PREFIXES = ("check:", "workflow:", "up_to_date")


class QueueEntry(BaseModel):
    number: int
    outcome: Literal["merge", "update_branch", "wait", "blocked"]
    ci_only: bool = False
    """For ``wait``: whether it waits only for its CI."""
    ci_seconds: float = 0.0
    """What running its CI again costs: the time its checks took at its head."""
    armed_at: datetime
    title: str = ""

    @property
    def ready(self) -> bool:
        """Whether it can take a turn: nothing but being behind keeps it from merging."""
        return self.outcome in ("merge", "update_branch")

    @property
    def keeps_turn(self) -> bool:
        """Whether, as the head, it keeps its turn."""
        return self.ready or (self.outcome == "wait" and self.ci_only)


def ci_seconds(pr: PrSnapshot) -> float:
    """The time the PR's checks took at its head, summed: the CI it runs again after
    it merges the base. A minute per check when the forge gave no times."""
    total = 0.0
    timed = False
    for check in pr.checks:
        if check.started_at and check.completed_at:
            total += max(0.0, (check.completed_at - check.started_at).total_seconds())
            timed = True
    return total if timed else 60.0 * len(pr.checks)


def entry_for(pr: PrSnapshot, gate: GateResult, armed_at: datetime) -> QueueEntry:
    failing = [i for i in gate.items if not i.ok]
    return QueueEntry(
        number=pr.number,
        outcome=gate.outcome,
        ci_only=bool(failing)
        and all(i.name.startswith(CI_ITEM_PREFIXES) for i in failing),
        ci_seconds=ci_seconds(pr),
        armed_at=armed_at,
        title=pr.title,
    )


def merge_order(entries: list[QueueEntry], head: int | None) -> list[QueueEntry]:
    """``entries`` in the order they merge, the current ``head`` first while it keeps
    its turn."""
    keep = next((e for e in entries if e.number == head and e.keeps_turn), None)
    rest = [e for e in entries if e is not keep]
    as_is = sorted(
        (e for e in rest if e.outcome == "merge"),
        key=lambda e: (-e.ci_seconds, e.armed_at, e.number),
    )
    update = sorted(
        (e for e in rest if e.outcome == "update_branch"),
        key=lambda e: (e.ci_seconds, e.armed_at, e.number),
    )
    later = sorted(
        (e for e in rest if not e.ready), key=lambda e: (e.armed_at, e.number)
    )
    return ([keep] if keep else []) + as_is + update + later


def next_head(entries: list[QueueEntry], head: int | None) -> int | None:
    """Whose turn it is: the first in ``merge_order`` if it may take one."""
    order = merge_order(entries, head)
    if order and (order[0].number == head and order[0].keeps_turn or order[0].ready):
        return order[0].number
    return None
