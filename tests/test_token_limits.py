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

"""Per-person token accounting and limits: what counts toward a person (their
requests on the service's agents), by kind of task, the rolling windows, who sees
whom, and only the owner of the service sets the limits."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.records import AgentRunRecord
from auto_maintainer.domain.settings import TokenLimit, TokenLimits
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def _run(h: Any, ago: timedelta, tokens: int, **kw: Any) -> None:
    await h.store.add_agent_run(
        AgentRunRecord(
            **{
                "id": f"run_{ago.total_seconds():.0f}_{tokens}_{kw.get('task_kind', '')}",
                "runtime": "claude-code",
                "task_kind": "chat",
                "ok": True,
                "input_tokens": tokens,
                "created_at": h.clock.now() - ago,
                **kw,
            }
        )
    )


def test_a_persons_own_limit_replaces_the_default_whole() -> None:
    limits = TokenLimits(
        default=TokenLimit(daily_tokens=1000),
        people={"Walt": TokenLimit(monthly_tokens=5), "maria": TokenLimit()},
    )
    assert limits.for_login("walt").windows() == [("month", 5)]
    assert limits.for_login("MARIA").windows() == [], "{} is no limit"
    assert limits.for_login("bob").windows() == [("day", 1000)]
    assert TokenLimits().for_login("anyone").windows() == [], "unset limits nobody"
    with pytest.raises(ValueError):
        TokenLimits(people={"@walt": TokenLimit()})


async def test_only_their_requests_on_the_service_agents_count(demo) -> None:
    h, _ = demo
    await _run(h, timedelta(hours=2), 600, requested_by="walt")
    await _run(h, timedelta(days=3), 300, requested_by="Walt", task_kind="review")
    await _run(h, timedelta(days=20), 100, requested_by="walt", task_kind="fix")
    await _run(h, timedelta(days=40), 9000, requested_by="walt")
    await _run(h, timedelta(hours=1), 5000, requested_by="walt", account="walt")
    await _run(h, timedelta(hours=1), 7000, task_kind="evaluate")
    ledger = h.service.ledger
    assert await ledger.used("walt") == {"day": 600, "week": 900, "month": 1000}
    assert await ledger.over("walt", TokenLimit(daily_tokens=1000)) is None
    reason = await ledger.over("walt", TokenLimit(weekly_tokens=900))
    assert reason and "900 of 900" in reason and "last week" in reason
    # A run recorded since the last read counts at once.
    await _run(h, timedelta(minutes=1), 500, requested_by="walt")
    assert (await ledger.used("walt"))["day"] == 1100


async def test_work_a_person_started_counts_as_theirs(demo) -> None:
    """A run whose task did not name its requester (recorded before its playbook
    passed it on) counts toward the person whose event started its job; the
    service's own work stays the service's, by repository and by day."""
    from auto_maintainer.domain.records import Job

    h, app = demo
    asked = Job(
        playbook="self_improve",
        key="k1",
        events=[{"kind": "dashboard.command", "payload": {"by": "walt"}}],
    )
    scan = Job(playbook="ci_doctor", key="k2", events=[{"kind": "checks.completed"}])
    for job in (asked, scan):
        await h.store.upsert_job(job)
    await _run(h, timedelta(hours=1), 800, task_kind="self_improve", job_id=asked.id)
    await _run(
        h,
        timedelta(hours=1),
        300,
        task_kind="ci_diagnose",
        job_id=scan.id,
        repo="acme/widget",
        cached_input_tokens=200,
    )
    assert (await h.service.ledger.used("walt"))["day"] == 800
    async with _as(app, "maria") as maria:
        view = (await maria.get("/api/people/usage?days=7")).json()
    [walt] = [p for p in view["people"] if p["login"] == "walt"]
    assert walt["service"]["by_kind"]["self_improve"]["tokens"] == 800
    service = view["background"]
    assert set(service["by_kind"]) == {"ci_diagnose"}
    assert service["by_repo"]["acme/widget"]["cached_input_tokens"] == 200
    today = (h.clock.now() - timedelta(hours=1)).date().isoformat()
    assert service["by_day"][today] == {"ci_diagnose": 300}


async def test_usage_by_person_and_kind_and_who_sees_it(demo) -> None:
    h, app = demo
    await _run(h, timedelta(hours=1), 100, requested_by="walt")
    await _run(h, timedelta(hours=1), 200, requested_by="walt", task_kind="review")
    await _run(
        h,
        timedelta(hours=1),
        40,
        requested_by="walt",
        task_kind="review",
        activity="review_talk",
    )
    await _run(h, timedelta(hours=1), 70, requested_by="walt", account="walt")
    await _run(h, timedelta(hours=1), 900, requested_by="bob", task_kind="fix")
    await _run(h, timedelta(hours=1), 3000, task_kind="evaluate")
    async with _as(app, "maria") as maria:
        everyone = (await maria.get("/api/people/usage?days=7")).json()
    rows = {p["login"].lower(): p for p in everyone["people"]}
    walt = rows["walt"]
    assert {k: v["tokens"] for k, v in walt["service"]["by_kind"].items()} == {
        "chat": 100,
        "review": 200,
        "review_talk": 40,
    }, "a review's discussion counts apart from writing the review"
    assert walt["own"]["input_tokens"] == 70 and walt["used"]["day"] == 340
    assert rows["bob"]["service"]["by_kind"]["fix"]["tokens"] == 900
    assert everyone["background"]["by_kind"]["evaluate"]["tokens"] == 3000
    assert everyone["can_manage"] is True

    async with _as(app, "walt") as walt_client:
        mine = (await walt_client.get("/api/people/usage")).json()
    assert [p["login"].lower() for p in mine["people"]] == ["walt"]
    assert mine["background"] is None and mine["can_manage"] is False


async def test_only_the_owner_sets_limits_and_they_apply_at_once(demo) -> None:
    h, app = demo
    body = {
        "limits": {
            "default": {"daily_tokens": 1000},
            "people": {"maria": {}},
        }
    }
    async with _as(app, "walt") as walt:
        proposed = (await walt.put("/api/settings/token-limits", json=body)).json()
        assert proposed["approved"] is False, "a proposal that waits for the owner"
        assert (await walt.get("/api/people/usage")).json()["limits"]["default"] == {
            "daily_tokens": None,
            "weekly_tokens": None,
            "monthly_tokens": None,
        }
    async with _as(app, "maria") as maria:
        saved = await maria.put("/api/settings/token-limits", json=body)
        assert saved.status_code == 200, saved.text
        view = (await maria.get("/api/people/usage")).json()
    assert view["limits"]["default"]["daily_tokens"] == 1000
    assert "maria" in view["limits"]["people"], "{} stays: no limit for maria"
    await _run(h, timedelta(minutes=5), 1500, requested_by="walt")
    assert "token limit" in (await h.service.over_limit("walt") or "")
    assert await h.service.over_limit("maria") is None
