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

"""Daily PR open/merge/backlog curve rules and API endpoint."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from auto_maintainer.domain.pr_activity import (
    build_issue_activity,
    build_pr_activity,
    build_star_history,
)
from auto_maintainer.domain.snapshot import (
    IssueSnapshot,
    IssueTimelineEntry,
    MergedPrSummary,
    PrState,
    PrTimelineEntry,
    StarHistoryRaw,
    StarPoint,
)
from auto_maintainer.testing.demo import DEMO_REPO, build_demo
from auto_maintainer.web.app import create_app


def test_build_pr_activity_counts_opened_merged_and_reconstructs_open_backlog() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    entries = [
        # Opened 2 days ago, still open today
        PrTimelineEntry(
            number=1,
            state=PrState.OPEN,
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        ),
        # Opened 2 days ago, merged 1 day ago
        PrTimelineEntry(
            number=2,
            state=PrState.MERGED,
            created_at=now - timedelta(days=2),
            merged_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        ),
        # Opened before window, closed without merge today
        PrTimelineEntry(
            number=3,
            state=PrState.CLOSED,
            created_at=now - timedelta(days=20),
            closed_at=now,
            updated_at=now,
        ),
        # Opened today, merged today
        PrTimelineEntry(
            number=4,
            state=PrState.MERGED,
            created_at=now,
            merged_at=now,
            updated_at=now,
        ),
    ]
    # Suppose 10 PRs are open right now at end of 2026-11-20.
    out = build_pr_activity("acme/widget", entries, now=now, days=3, current_open=10)
    assert [d.day for d in out.series] == ["2026-11-18", "2026-11-19", "2026-11-20"]
    assert [(d.opened, d.merged, d.closed) for d in out.series] == [
        (2, 0, 0),
        (0, 1, 0),
        (1, 1, 1),
    ]
    # Backwards check:
    # End of 11-20: 10
    # End of 11-19: 10 - 1(opened) + 1(merged) + 1(closed) = 11
    # End of 11-18: 11 - 0 + 1(merged) + 0 = 12
    assert [d.open_count for d in out.series] == [12, 11, 10]
    assert out.total_opened == 3
    assert out.total_merged == 2
    assert out.total_closed == 1
    assert out.current_open == 10


def test_build_issue_activity_and_star_history_daily_and_all_time() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    issues = [
        IssueTimelineEntry(
            number=10,
            state="OPEN",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        ),
        IssueTimelineEntry(
            number=11,
            state="CLOSED",
            created_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        ),
        IssueTimelineEntry(
            number=12,
            state="CLOSED",
            created_at=now - timedelta(days=30),
            closed_at=now,
            updated_at=now,
        ),
    ]
    issue_out = build_issue_activity(
        "acme/widget", issues, now=now, days=3, current_open=25
    )
    assert [(d.opened, d.closed, d.open_count) for d in issue_out.series] == [
        (2, 0, 27),
        (0, 1, 26),
        (0, 1, 25),
    ]
    assert issue_out.total_opened == 2 and issue_out.total_closed == 2

    raw_stars = StarHistoryRaw(
        total=500,
        recent=[
            now - timedelta(days=2),
            now - timedelta(days=2),
            now,
        ],
        sampled=[
            StarPoint(at=datetime(2025, 8, 17, tzinfo=UTC), count=1),
            StarPoint(at=datetime(2026, 3, 1, tzinfo=UTC), count=250),
            StarPoint(at=datetime(2026, 11, 19, tzinfo=UTC), count=499),
        ],
    )
    daily = build_star_history("acme/widget", raw_stars, now=now, days=3)
    assert [(d.day, d.added, d.total) for d in daily.series] == [
        ("2026-11-18", 2, 499),
        ("2026-11-19", 0, 499),
        ("2026-11-20", 1, 500),
    ]
    assert daily.added_in_period == 3 and not daily.sampled

    all_time = build_star_history("acme/widget", raw_stars, now=now, days=None)
    assert all_time.sampled is True and all_time.total == 500
    assert [(d.day, d.total) for d in all_time.series] == [
        ("2025-08-17", 1),
        ("2026-03-01", 250),
        ("2026-11-19", 499),
        ("2026-11-20", 500),
    ]


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_repo_pr_activity_endpoint_returns_series(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        now = h.clock.now()
        h.forge.merged.append(
            MergedPrSummary(
                number=999,
                title="merged yesterday",
                author="bob",
                merged_at=now - timedelta(days=1),
            )
        )
        h.forge.issues[(DEMO_REPO, 501)] = IssueSnapshot(
            repo=DEMO_REPO,
            number=501,
            title="bug",
            author="bob",
            created_at=now - timedelta(days=2),
        )
        h.forge.stars[DEMO_REPO] = [
            now - timedelta(days=10),
            now - timedelta(days=1),
            now,
        ]
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with _as(app, "maria") as maria:
            res = await maria.get(f"/api/repos/{DEMO_REPO}/pr-activity?days=14")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["repo"] == DEMO_REPO
            assert body["days"] == 14
            assert len(body["series"]) == 14
            assert body["total_merged"] >= 1
            assert body["current_open"] >= 1
            assert body["series"][-1]["open_count"] == body["current_open"]

            iss = await maria.get(f"/api/repos/{DEMO_REPO}/issue-activity?days=14")
            assert iss.status_code == 200, iss.text
            ibody = iss.json()
            assert ibody["days"] == 14 and ibody["total_opened"] >= 1
            assert ibody["current_open"] >= 1

            stars_all = await maria.get(f"/api/repos/{DEMO_REPO}/star-history")
            assert stars_all.status_code == 200, stars_all.text
            sall = stars_all.json()
            assert sall["total"] == 3 and sall["series"][-1]["total"] == 3

            stars_14 = await maria.get(f"/api/repos/{DEMO_REPO}/star-history?days=14")
            assert stars_14.status_code == 200, stars_14.text
            s14 = stars_14.json()
            assert s14["days"] == 14 and s14["added_in_period"] == 3
    finally:
        await h.close()
