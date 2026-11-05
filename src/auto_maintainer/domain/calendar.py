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

"""Business-day arithmetic for SLA and stale timers."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .profile import Timing


class BusinessCalendar:
    """Weekends and holidays from a profile's ``timing`` section."""

    def __init__(self, timing: Timing) -> None:
        self._holidays = set(timing.holidays)
        self._workdays = set(timing.workdays)
        self._weekend = set(timing.weekend)

    def is_business_day(self, day: date) -> bool:
        if day in self._workdays:
            return True
        return day.weekday() not in self._weekend and day not in self._holidays

    def business_days_between(self, start: datetime, end: datetime) -> int:
        """Whole business days after ``start``'s day, up to and including ``end``'s day."""
        if end <= start:
            return 0
        count = 0
        day = start.date() + timedelta(days=1)
        while day <= end.date():
            if self.is_business_day(day):
                count += 1
            day += timedelta(days=1)
        return count
