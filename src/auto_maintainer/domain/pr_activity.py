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

"""Daily pull-request open and merge counts for a repository (pure rules)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from .snapshot import PrState, PrTimelineEntry


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrActivityDay(_Model):
    """PR counts for one UTC day."""

    day: str
    """ISO date (``YYYY-MM-DD``, UTC)."""
    opened: int = 0
    """Pull requests created on this day."""
    merged: int = 0
    """Pull requests merged on this day."""
    closed: int = 0
    """Pull requests closed without merging on this day."""
    open_count: int = 0
    """Open pull requests at the end of this day."""


class PrActivitySeries(_Model):
    """A repository's daily PR opened, merged, and open-backlog curve."""

    repo: str
    days: int
    series: list[PrActivityDay] = Field(default_factory=list)
    total_opened: int = 0
    total_merged: int = 0
    total_closed: int = 0
    current_open: int = 0


def _utc_day(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(UTC).date().isoformat()


def build_pr_activity(
    repo: str,
    entries: Iterable[PrTimelineEntry],
    *,
    now: datetime,
    days: int = 30,
    current_open: int | None = None,
) -> PrActivitySeries:
    """Bucket PR lifecycle entries into ``days`` consecutive UTC days ending at ``now``.

    ``open_count`` at the end of each day is reconstructed backwards from
    ``current_open`` (the exact open PR count today):
    ``open[d - 1] = max(0, open[d] - opened[d] + merged[d] + closed[d])``.
    """
    span = max(1, min(days, 365))
    end_date = (now.astimezone(UTC) if now.tzinfo else now).date()
    start_date = end_date - timedelta(days=span - 1)
    day_keys = [(start_date + timedelta(days=i)).isoformat() for i in range(span)]
    index = {d: i for i, d in enumerate(day_keys)}

    dedup: dict[int, PrTimelineEntry] = {}
    for entry in entries:
        prev = dedup.get(entry.number)
        if prev is None:
            dedup[entry.number] = entry
        else:
            dedup[entry.number] = PrTimelineEntry(
                number=entry.number,
                state=entry.state,
                created_at=entry.created_at or prev.created_at,
                merged_at=entry.merged_at or prev.merged_at,
                closed_at=entry.closed_at or prev.closed_at,
                updated_at=entry.updated_at or prev.updated_at,
            )

    buckets = [PrActivityDay(day=d) for d in day_keys]
    for e in dedup.values():
        opened_day = _utc_day(e.created_at)
        if opened_day in index:
            buckets[index[opened_day]].opened += 1
        merged_day = _utc_day(e.merged_at)
        if merged_day in index:
            buckets[index[merged_day]].merged += 1
        elif e.state == PrState.CLOSED:
            closed_day = _utc_day(e.closed_at)
            if closed_day in index:
                buckets[index[closed_day]].closed += 1

    open_now = (
        current_open
        if current_open is not None
        else sum(1 for e in dedup.values() if e.state == PrState.OPEN)
    )
    running = max(0, open_now)
    for i in range(len(buckets) - 1, -1, -1):
        buckets[i].open_count = running
        running = max(
            0, running - buckets[i].opened + buckets[i].merged + buckets[i].closed
        )

    return PrActivitySeries(
        repo=repo,
        days=span,
        series=buckets,
        total_opened=sum(b.opened for b in buckets),
        total_merged=sum(b.merged for b in buckets),
        total_closed=sum(b.closed for b in buckets),
        current_open=max(0, open_now),
    )
