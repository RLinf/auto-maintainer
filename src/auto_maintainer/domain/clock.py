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

"""Clocks. All times are timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC, treating a naive datetime as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Clock(Protocol):
    """Source of the current time."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return utc_now()


class FixedClock:
    """A clock that only moves when told to; for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = as_utc(start or datetime(2026, 1, 5, 9, 0, tzinfo=UTC))

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        """Move the clock forward by ``timedelta(**kwargs)`` and return the new time."""
        self._now += timedelta(**kwargs)
        return self._now
