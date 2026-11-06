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

"""Run CI: the one step that gets CI going on a PR's head, run as the person who
pressed it."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.run_ci import plan_run_ci
from auto_maintainer.domain.snapshot import CheckConclusion, CheckRun, CheckStatus
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


def _check(run_id: int, status: str, conclusion: str | None, attempt: int = 1):
    return CheckRun(
        id=run_id * 10 + attempt,
        name=f"job-{run_id}",
        workflow=f"wf-{run_id}",
        status=CheckStatus(status),
        conclusion=CheckConclusion(conclusion) if conclusion else None,
        run_id=run_id,
        run_attempt=attempt,
    )


@pytest.mark.parametrize(
    ("labels", "checks", "state", "reruns"),
    [
        ([], [_check(1, "completed", "success")], "gated", []),
        (["run-ci"], [_check(1, "completed", "failure", 2)], "failed", [(1, 2)]),
        (
            ["run-ci"],
            [_check(1, "completed", "failure"), _check(2, "in_progress", None)],
            "failed",
            [(1, 1)],
        ),
        (["run-ci"], [_check(2, "in_progress", None)], "running", []),
        (["run-ci"], [_check(1, "completed", "success")], "passed", []),
        (["run-ci"], [], "none", []),
    ],
)
def test_run_ci_does_the_one_step_ci_needs(
    labels: list[str], checks: list[CheckRun], state: str, reruns: list
) -> None:
    pr = make_pr(labels=labels, checks=checks)
    plan = plan_run_ci(pr, "run-ci")
    assert plan.state == state
    assert [(r.run_id, r.attempt) for r in plan.reruns] == reruns
    assert plan.can_run is (state in ("gated", "failed"))


def test_without_a_gate_label_failed_runs_are_rerun() -> None:
    pr = make_pr(labels=[], checks=[_check(3, "completed", "timed_out")])
    plan = plan_run_ci(pr, None)
    assert plan.state == "failed" and plan.reruns[0].run_id == 3


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
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


async def test_pressing_run_ci_adds_the_gate_then_reruns_what_failed(demo) -> None:
    h, app = demo
    async with _as(app, "walt") as walt:
        detail = (await walt.get(f"/api/prs/{REPO}/43")).json()
        assert detail["run_ci"]["state"] == "gated"
        assert detail["run_ci"]["label"] == "run-ci"
        first = await walt.post(f"/api/prs/{REPO}/43/ci")
        assert first.status_code == 200, first.text
        await h.settle()
        assert "run-ci" in h.forge.prs[(REPO, 43)].labels
        # the PR page sees it at once, and now offers to rerun the failed run
        detail = (await walt.get(f"/api/prs/{REPO}/43")).json()
        assert "run-ci" in detail["pr"]["labels"]
        assert detail["run_ci"]["state"] == "failed"
        second = await walt.post(f"/api/prs/{REPO}/43/ci")
        assert second.status_code == 200, second.text
        await h.settle()
        assert [w["run_id"] for w in h.forge.writes_of("rerun_failed_jobs")] == [11]
        # pressing again at the same attempt does not rerun twice
        await walt.post(f"/api/prs/{REPO}/43/ci")
        await h.settle()
        assert len(h.forge.writes_of("rerun_failed_jobs")) == 1
        audit = (await walt.get(f"/api/audit?repo={REPO}")).json()
        assert any(a["actor"] == "walt" and "approved" in a["summary"] for a in audit)


async def test_there_is_nothing_to_run_when_ci_passed(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/ci")
        await h.settle()
        detail = (await maria.get(f"/api/prs/{REPO}/42")).json()
        assert detail["run_ci"]["state"] == "passed"
        again = await maria.post(f"/api/prs/{REPO}/42/ci")
        assert again.status_code == 409 and "passed" in again.text
    async with _as(app, "eve") as eve:
        assert (await eve.post(f"/api/prs/{REPO}/42/ci")).status_code == 403


async def test_a_maintainer_turns_on_automatic_flaky_reruns_in_one_step(demo) -> None:
    from auto_maintainer.domain.actions import ActionKind, Autonomy

    h, app = demo
    async with _as(app, "walt") as walt:
        view = (await walt.get(f"/api/repos/{REPO}/agreement")).json()
        assert view["levels"]["ci.rerun_failed"] == "auto"  # the demo already runs it
        assert view["levels"]["lab.reproduce"] == "shadow"
        assert view["can_configure"] is False
        proposed = (
            await walt.put(
                f"/api/repos/{REPO}/policy/label.ci_gate", json={"level": "auto"}
            )
        ).json()
        assert proposed["applied"] is False
        assert (
            await walt.put(
                f"/api/repos/{REPO}/policy/memory.commit", json={"level": "auto"}
            )
        ).status_code == 422
        assert (
            await walt.put(f"/api/repos/{REPO}/policy/nothing", json={"level": "auto"})
        ).status_code == 404
    async with _as(app, "maria") as maria:
        applied = (
            await maria.put(
                f"/api/repos/{REPO}/policy/label.ci_gate", json={"level": "auto"}
            )
        ).json()
        assert applied["applied"] is True
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    assert profile.policy[ActionKind.LABEL_CI_GATE].level == Autonomy.AUTO
