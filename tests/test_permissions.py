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

"""Who may do what in a repository (design 2.3): its People record's maintainers, or
else its GitHub admins, configure it, assign reviewers, and decide every proposal; a
reviewer decides what they asked for, claims PRs alongside others, and sees their own
agent usage; a person approves a PR only as themselves."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from auto_maintainer.actuator.pipeline import ApprovalError
from auto_maintainer.domain.actions import (
    ActionKind,
    ApprovePrPayload,
    LabelPayload,
    Provenance,
    make_action,
)
from auto_maintainer.domain.ids import new_id
from auto_maintainer.domain.memory import profile_path
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.records import AgentRunRecord, Job
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.profiles.governance import governance_path
from auto_maintainer.profiles.layers import dump_yaml, parse_yaml
from auto_maintainer.testing.demo import DEMO_GOVERNANCE, build_demo
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


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


async def _levels(h: Harness, **levels: str) -> None:
    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("policy", {}).update(
        {k.replace("_", ".", 1): {"level": v} for k, v in levels.items()}
    )
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")


def _record(**members: str) -> str:
    """The demo People record with ``members`` (login: role) instead of its own."""
    return yaml.safe_dump(
        {
            **DEMO_GOVERNANCE,
            "repo": REPO,
            "members": [{"login": k, "role": v} for k, v in members.items()],
        },
        sort_keys=False,
    )


async def _configures(h: Harness, login: str) -> bool:
    profile = await h.service.profiles.active(REPO)
    return (await h.service.gate.check(REPO, login, profile, person=True)).allowed


async def test_the_people_record_names_who_maintains_a_repository(demo) -> None:
    h, _ = demo
    h.forge.set_permission(REPO, "rita", Permission.READ)
    h.memory.files.pop(governance_path(REPO), None)
    # No record: the repository's GitHub admins maintain it.
    assert await _configures(h, "maria")
    assert not await _configures(h, "walt"), "write access is not enough"

    h.memory.files[governance_path(REPO)] = _record(rita="maintainer", walt="reviewer")
    assert await _configures(h, "rita"), "a maintainer in the record, with read"
    assert not await _configures(h, "walt"), "a reviewer in the record"
    assert not await _configures(h, "maria"), "an admin the record does not name"
    h.forge.set_permission(REPO, "rita", Permission.NONE)
    assert not await _configures(h, "rita"), "one who lost access to the repository"


async def test_members_of_the_record_see_the_repository_with_read_access(demo) -> None:
    h, app = demo
    h.forge.set_permission(REPO, "rita", Permission.READ)
    h.forge.set_permission(REPO, "olga", Permission.READ)
    h.memory.files[governance_path(REPO)] = _record(maria="maintainer", rita="reviewer")
    async with _as(app, "rita") as rita:
        assert (await rita.get(f"/api/prs?repo={REPO}")).status_code == 200
    async with _as(app, "olga") as olga:
        assert (await olga.get(f"/api/prs?repo={REPO}")).status_code == 403


async def test_a_reviewer_decides_only_what_they_asked_for(demo) -> None:
    h, app = demo
    await _levels(h, label_apply="propose")

    async def propose(by: str | None, label: str) -> str:
        record = await h.service.pipeline.submit(
            make_action(
                ActionKind.LABEL_APPLY,
                REPO,
                LabelPayload(labels=[label]),
                number=42,
                discriminator=new_id("t"),
                provenance=Provenance(requested_by=by),
            )
        )
        proposal = await h.store.proposal_for_action(record.action.id)
        assert proposal is not None
        return proposal.id

    walts = await propose("walt", "bug")
    services = await propose(None, "documentation")
    async with _as(app, "walt") as walt:
        rows = {p["id"]: p for p in (await walt.get("/api/proposals")).json()}
        assert rows[walts]["can_decide"] and rows[walts]["requester"] == "walt"
        assert not rows[services]["can_decide"]
        refused = await walt.post(f"/api/proposals/{services}/approve", json={})
        assert refused.status_code == 403
        refused = await walt.post(
            f"/api/proposals/{services}/reject", json={"reason": "no"}
        )
        assert refused.status_code == 403
        assert (
            await walt.post(f"/api/proposals/{walts}/approve", json={})
        ).status_code == 200
    async with _as(app, "maria") as maria:
        rows = {p["id"]: p for p in (await maria.get("/api/proposals")).json()}
        assert rows[services]["can_decide"], "a maintainer decides every proposal"
        assert (
            await maria.post(f"/api/proposals/{services}/approve", json={})
        ).status_code == 200


async def test_reviewers_claim_together_and_maintainers_assign_anyone(demo) -> None:
    h, app = demo
    h.forge.set_permission(REPO, "newbie", Permission.READ)
    h.memory.files[governance_path(REPO)] = _record(maria="maintainer", bob="reviewer")
    base = f"/api/prs/{REPO}/42"
    async with _as(app, "walt") as walt:
        await walt.post(f"{base}/claim")
        refused = await walt.post(f"{base}/assignees", json={"logins": ["newbie"]})
        assert refused.status_code == 403
    async with _as(app, "maria") as maria:
        await maria.post(f"{base}/claim")
        unknown = await maria.post(f"{base}/assignees", json={"logins": ["ghost"]})
        assert unknown.status_code == 422, "a login GitHub does not know"
        bad = await maria.post(f"{base}/assignees", json={"logins": ["no spaces"]})
        assert bad.status_code == 422
        out = (
            await maria.post(f"{base}/assignees", json={"logins": ["@newbie", "walt"]})
        ).json()
        assert out["requested"]["status"] in {"queued", "executed"}
        assignees = {a["login"]: a for a in out["assignees"]}
        assert set(assignees) == {"walt", "maria", "newbie"}
        assert assignees["newbie"] == {**assignees["newbie"], "kind": "assigned"}
        assert assignees["newbie"]["by"] == "maria"
        assert assignees["walt"]["kind"] == "assigned", "an assignment over a claim"
        await h.settle()
        [request] = [w for w in h.forge.writes if w[0] == "request_reviewers"]
        assert set(request[1]["logins"]) == {"newbie", "walt"}
        who = (await maria.get(f"/api/repos/{REPO}/assignable")).json()
        assert who["can_assign"] and "bob" in [p["login"] for p in who["people"]]
        [row] = [r for r in (await maria.get("/api/prs")).json() if r["number"] == 42]
        assert {a["login"] for a in row["assignees"]} == {"walt", "maria", "newbie"}
    async with _as(app, "walt") as walt:
        assert (await walt.delete(f"{base}/assignees/maria")).status_code == 403
        assert (await walt.delete(f"{base}/claim")).status_code == 200
        mine = (await walt.get("/api/prs?mine=true")).json()
        assert 42 not in [r["number"] for r in mine]
        assert not (await walt.get(f"/api/repos/{REPO}/assignable")).json()[
            "can_assign"
        ]
    async with _as(app, "maria") as maria:
        left = (await maria.delete(f"{base}/assignees/newbie")).json()
        assert [a["login"] for a in left["assignees"]] == ["maria"]


async def test_usage_counts_each_persons_runs_in_a_repository(demo) -> None:
    h, app = demo
    job = Job(
        playbook="review",
        key="k",
        repo=REPO,
        events=[{"id": "evt_1", "payload": {"by": "walt"}}],
    )
    job, _ = await h.store.upsert_job(job)

    def run(tokens: int, by: str | None = None, job_id: str | None = None) -> Any:
        return AgentRunRecord(
            id=new_id("run"),
            repo=REPO,
            runtime="claude",
            task_kind="review",
            ok=True,
            input_tokens=tokens,
            output_tokens=10,
            cost_usd=0.5,
            requested_by=by,
            job_id=job_id,
            created_at=h.clock.now(),
        )

    for r_ in [run(1000, "maria"), run(300, "walt"), run(200, job_id=job.id), run(50)]:
        await h.store.add_agent_run(r_)
    await h.store.add_agent_run(
        run(9999, "maria").model_copy(update={"repo": "acme/other"})
    )
    async with _as(app, "maria") as maria:
        u = (await maria.get(f"/api/repos/{REPO}/usage")).json()
    assert u["everyone"] and u["total"]["runs"] == 4
    rows = {r["login"]: r for r in u["people"]}
    assert rows["maria"]["input_tokens"] == 1000 and rows["maria"]["cost_usd"] == 0.5
    assert rows["walt"]["runs"] == 2, "a run without a requester counts for its job's"
    assert rows[None]["input_tokens"] == 50, "the service's own work"
    async with _as(app, "walt") as walt:
        u = (await walt.get(f"/api/repos/{REPO}/usage")).json()
    assert not u["everyone"] and u["total"]["runs"] == 4
    assert [r["login"] for r in u["people"]] == ["walt"]


async def test_a_person_approves_a_pr_only_as_themselves(demo) -> None:
    h, app = demo
    h.forge.put_pr(make_pr(number=7, author="alice"))
    url = f"/api/prs/{REPO}/7/approve"
    async with _as(app, "maria") as maria:
        refused = await maria.post(url, json={})
        assert refused.status_code == 403, "without a sign-in to approve with"
        await h.service.a.user_tokens.set("maria", UserToken(token="t"))
        await _levels(h, pr_approve="off")
        off = (await maria.post(url, json={"head": "a" * 40})).json()
        assert off["status"] == "denied", "a repository can turn the button off"
        # Shadow, the default, holds only the service's own work: a press runs.
        await _levels(h, pr_approve="shadow")
        moved = await maria.post(url, json={"head": "b" * 40})
        assert moved.status_code == 409, "a head the person did not look at"
        out = (
            await maria.post(url, json={"body": "Looks good.", "head": "a" * 40})
        ).json()
        assert out["status"] in {"queued", "executed"}, out
        await h.settle()
        [approval] = [w for w in h.forge.writes if w[0] == "approve_pr"]
        assert approval[1]["body"] == "Looks good."
        [review] = h.forge.prs[(REPO, 7)].reviews
        assert (review.author, review.state) == ("maria", "APPROVED")
        again = await maria.post(url, json={})
        assert again.status_code == 409, "one approval per head"

    # The service's own account never approves, whatever approves the action.
    with pytest.raises(PermissionError):
        await h.forge.approve_pr(REPO, 7, "a" * 40, None)  # type: ignore[arg-type]
    await _levels(h, pr_approve="propose")
    record = await h.service.pipeline.submit(
        make_action(
            ActionKind.PR_APPROVE,
            REPO,
            ApprovePrPayload(),
            number=7,
            head_sha="a" * 40,
            discriminator="direct",
        )
    )
    proposal = await h.store.proposal_for_action(record.action.id)
    assert proposal is not None
    with pytest.raises(ApprovalError):
        await h.service.pipeline.approve(proposal.id, "maria")


async def test_a_prs_owners_include_who_the_forge_assigned_or_asked(demo) -> None:
    """A PR's owners are who claimed it or was assigned on the dashboard, its
    assignees on the forge, and the people asked to review it there (not teams);
    "mine" means a PR the person owns."""
    h, app = demo
    pr = make_pr(
        number=77,
        requested_reviewers=["walt"],
        requested_teams=["core-team"],
        assignees=["bob", "maria"],
    )
    h.forge.put_pr(pr)
    await h.store.save_snapshot(pr)
    h.forge.set_permission(REPO, "carol", Permission.WRITE)
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/77/claim")
        [row] = [r for r in (await maria.get("/api/prs")).json() if r["number"] == 77]
        assert [(o["login"], o["kind"]) for o in row["owners"]] == [
            ("maria", "claimed"),
            ("bob", "assignee"),
            ("walt", "requested"),
        ]
    async with _as(app, "walt") as walt:
        mine = (await walt.get("/api/prs?mine=true")).json()
        assert 77 in [r["number"] for r in mine]
    async with _as(app, "carol") as carol:
        assert 77 in [r["number"] for r in (await carol.get("/api/prs")).json()]
        mine = (await carol.get("/api/prs?mine=true")).json()
        assert 77 not in [r["number"] for r in mine]
