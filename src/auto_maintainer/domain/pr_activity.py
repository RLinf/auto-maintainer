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

from .snapshot import (
    IssueTimelineEntry,
    PrState,
    PrTimelineEntry,
    StarHistoryRaw,
)


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


class IssueActivityDay(_Model):
    """Issue counts for one UTC day."""

    day: str
    opened: int = 0
    closed: int = 0
    open_count: int = 0


class IssueActivitySeries(_Model):
    """A repository's daily issue opened, closed, and open-backlog curve."""

    repo: str
    days: int
    series: list[IssueActivityDay] = Field(default_factory=list)
    total_opened: int = 0
    total_closed: int = 0
    current_open: int = 0


def build_issue_activity(
    repo: str,
    entries: Iterable[IssueTimelineEntry],
    *,
    now: datetime,
    days: int = 30,
    current_open: int | None = None,
) -> IssueActivitySeries:
    """Bucket issue lifecycle entries into ``days`` consecutive UTC days ending at
    ``now``, reconstructing end-of-day ``open_count`` backwards from ``current_open``."""
    span = max(1, min(days, 365))
    end_date = (now.astimezone(UTC) if now.tzinfo else now).date()
    start_date = end_date - timedelta(days=span - 1)
    day_keys = [(start_date + timedelta(days=i)).isoformat() for i in range(span)]
    index = {d: i for i, d in enumerate(day_keys)}

    dedup: dict[int, IssueTimelineEntry] = {}
    for entry in entries:
        prev = dedup.get(entry.number)
        if prev is None:
            dedup[entry.number] = entry
        else:
            dedup[entry.number] = IssueTimelineEntry(
                number=entry.number,
                state=entry.state,
                created_at=entry.created_at or prev.created_at,
                closed_at=entry.closed_at or prev.closed_at,
                updated_at=entry.updated_at or prev.updated_at,
            )

    buckets = [IssueActivityDay(day=d) for d in day_keys]
    for e in dedup.values():
        opened_day = _utc_day(e.created_at)
        if opened_day in index:
            buckets[index[opened_day]].opened += 1
        if e.state.upper() == "CLOSED":
            closed_day = _utc_day(e.closed_at)
            if closed_day in index:
                buckets[index[closed_day]].closed += 1

    open_now = (
        current_open
        if current_open is not None
        else sum(1 for e in dedup.values() if e.state.upper() == "OPEN")
    )
    running = max(0, open_now)
    for i in range(len(buckets) - 1, -1, -1):
        buckets[i].open_count = running
        running = max(0, running - buckets[i].opened + buckets[i].closed)

    return IssueActivitySeries(
        repo=repo,
        days=span,
        series=buckets,
        total_opened=sum(b.opened for b in buckets),
        total_closed=sum(b.closed for b in buckets),
        current_open=max(0, open_now),
    )


class StarDay(_Model):
    """Star count on one UTC day."""

    day: str
    added: int = 0
    total: int = 0


class StarHistorySeries(_Model):
    """A repository's star history, either daily over the last ``days`` days or
    all-time sampled from star #1 to today."""

    repo: str
    days: int | None = None
    total: int = 0
    added_in_period: int = 0
    sampled: bool = False
    series: list[StarDay] = Field(default_factory=list)


def build_star_history(
    repo: str,
    raw: StarHistoryRaw,
    *,
    now: datetime,
    days: int | None = None,
) -> StarHistorySeries:
    """Build a star history curve from ``raw``: daily for ``days`` (14/30/90), or
    all-time cumulative when ``days is None``."""
    end_date = (now.astimezone(UTC) if now.tzinfo else now).date()
    total_now = max(0, raw.total)

    if days is not None:
        span = max(1, min(days, 365))
        start_date = end_date - timedelta(days=span - 1)
        day_keys = [(start_date + timedelta(days=i)).isoformat() for i in range(span)]
        index = {d: i for i, d in enumerate(day_keys)}
        buckets = [StarDay(day=d) for d in day_keys]
        for dt in raw.recent:
            d = _utc_day(dt)
            if d in index:
                buckets[index[d]].added += 1
        running = total_now
        for i in range(len(buckets) - 1, -1, -1):
            buckets[i].total = running
            running = max(0, running - buckets[i].added)
        return StarHistorySeries(
            repo=repo,
            days=span,
            total=total_now,
            added_in_period=sum(b.added for b in buckets),
            sampled=False,
            series=buckets,
        )

    # All-time curve: prefer sampled milestones if available, else build from exact
    # recent timestamps (e.g. small repositories or fakes).
    by_day: dict[str, int] = {}
    if raw.sampled:
        for p in sorted(raw.sampled, key=lambda x: (x.at, x.count)):
            d = _utc_day(p.at)
            if d is not None:
                by_day[d] = max(by_day.get(d, 0), p.count)
        sampled = True
    elif raw.recent:
        ordered = sorted(raw.recent)
        base = max(0, total_now - len(ordered))
        for i, dt in enumerate(ordered, start=1):
            d = _utc_day(dt)
            if d is not None:
                by_day[d] = base + i
        sampled = False
    else:
        sampled = False

    today_key = end_date.isoformat()
    if total_now > 0 or by_day:
        by_day[today_key] = max(by_day.get(today_key, 0), total_now)

    points: list[StarDay] = []
    prev_total = 0
    for d in sorted(by_day):
        val = max(prev_total, by_day[d])
        points.append(StarDay(day=d, added=val - prev_total, total=val))
        prev_total = val

    return StarHistorySeries(
        repo=repo,
        days=None,
        total=total_now,
        added_in_period=total_now,
        sampled=sampled,
        series=points,
    )
