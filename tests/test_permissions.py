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
from auto_maintainer.domain.snapshot import PrFile, RepoInfo
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
        reviewing = (await walt.get("/api/prs?reviewing=true")).json()
        assert 42 not in [r["number"] for r in reviewing]
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


async def test_the_pr_page_lists_code_owners_apart_from_its_owners(demo) -> None:
    """The code owners of the changed files come from CODEOWNERS on the PR's base
    branch, with whether the branch waits for their approval; anyone who may see
    the repository reads them, and they never become the PR's owners."""
    h, app = demo
    h.forge.add_repo(RepoInfo(name=REPO, requires_code_owner_reviews=True))
    h.forge.put_file(REPO, ".github/CODEOWNERS", "* @lead\n/docs/ @writer\n")
    pr = make_pr(
        number=78,
        files=[PrFile(path="docs/a.md"), PrFile(path="main.py")],
        requested_reviewers=["writer", "walt"],
    )
    h.forge.put_pr(pr)
    await h.store.save_snapshot(pr)
    async with _as(app, "walt") as walt:
        got = (await walt.get(f"/api/prs/{REPO}/78/code-owners")).json()
        assert got["file"] == ".github/CODEOWNERS" and got["required"] is True
        assert [(p["owners"], p["files"]) for p in got["parts"]] == [
            (["writer"], ["docs/a.md"]),
            (["lead"], ["main.py"]),
        ]
        writer = next(p for p in got["people"] if p["login"] == "writer")
        assert writer["requested"] and got["requested"] == ["walt"]
        mine = (await walt.get("/api/prs?mine=true")).json()
        assert 78 not in [r["number"] for r in mine]
    async with _as(app, "nobody") as nobody:
        res = await nobody.get(f"/api/prs/{REPO}/78/code-owners")
        assert res.status_code in (403, 404)


async def test_a_prs_owners_are_only_who_claimed_or_was_assigned_here(demo) -> None:
    """A PR's owners and reviewers are who claimed the role or was assigned it on
    the dashboard. Reviews GitHub requested (its code owners' automatic ones among
    them) and the forge's assignees give nobody a role, so neither "mine" (owned)
    nor "reviewing" lists those PRs."""
    h, app = demo
    pr = make_pr(
        number=77,
        requested_reviewers=["walt"],
        requested_teams=["core-team"],
        assignees=["bob"],
    )
    h.forge.put_pr(pr)
    await h.store.save_snapshot(pr)
    h.forge.set_permission(REPO, "bob", Permission.WRITE)
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/77/claim", json={"role": "owner"})
        [row] = [r for r in (await maria.get("/api/prs")).json() if r["number"] == 77]
        assert [(a["login"], a["role"], a["kind"]) for a in row["assignees"]] == [
            ("maria", "owner", "claimed")
        ]
        assert "owners" not in row
        mine = (await maria.get("/api/prs?mine=true")).json()
        assert 77 in [r["number"] for r in mine]
        reviewing = (await maria.get("/api/prs?reviewing=true")).json()
        assert 77 not in [r["number"] for r in reviewing]
    for login in ("walt", "bob"):
        async with _as(app, login) as person:
            assert 77 in [r["number"] for r in (await person.get("/api/prs")).json()]
            for view in ("mine", "reviewing"):
                listed = (await person.get(f"/api/prs?{view}=true")).json()
                assert 77 not in [r["number"] for r in listed]


async def test_a_prs_owner_assigns_its_reviewers_but_not_its_owners(demo) -> None:
    """Owners answer for the whole PR: they assign and take off its reviewers.
    Only maintainers assign owners; assigning an owner asks GitHub for nothing,
    and one person may own a PR and review it."""
    h, app = demo
    h.forge.set_permission(REPO, "newbie", Permission.READ)
    base = f"/api/prs/{REPO}/42"
    async with _as(app, "walt") as walt:
        refused = await walt.post(
            f"{base}/assignees", json={"logins": ["newbie"], "role": "reviewer"}
        )
        assert refused.status_code == 403, "not yet the PR's owner"
    async with _as(app, "maria") as maria:
        owner = await maria.post(
            f"{base}/assignees", json={"logins": ["walt"], "role": "owner"}
        )
        assert owner.json()["requested"] is None, "an owner is not asked to review"
    async with _as(app, "walt") as walt:
        assert (
            await walt.post(
                f"{base}/assignees", json={"logins": ["newbie"], "role": "owner"}
            )
        ).status_code == 403
        assigned = await walt.post(
            f"{base}/assignees", json={"logins": ["newbie", "walt"], "role": "reviewer"}
        )
        assert assigned.status_code == 200
        roles = {(a["login"], a["role"]) for a in assigned.json()["assignees"]}
        assert roles == {
            ("walt", "owner"),
            ("walt", "reviewer"),
            ("newbie", "reviewer"),
        }
        left = await walt.delete(f"{base}/assignees/newbie?role=reviewer")
        assert {a["login"] for a in left.json()["assignees"]} == {"walt"}
        await walt.delete(f"{base}/claim?role=owner")
        assert (
            await walt.post(
                f"{base}/assignees", json={"logins": ["newbie"], "role": "reviewer"}
            )
        ).status_code == 403, "no longer the owner"
    await h.settle()
    [request] = [w for w in h.forge.writes if w[0] == "request_reviewers"]
    assert set(request[1]["logins"]) == {"newbie", "walt"}


async def test_people_choose_their_own_display_names_and_everyone_sees_them(
    demo,
) -> None:
    """The owner names someone who has no name yet; that person's own choice
    replaces it, and the owner cannot change it back. Nobody else names others, and
    two people never share a name."""
    h, app = demo
    h.forge.set_permission(REPO, "bob", Permission.WRITE)
    async with _as(app, "maria") as maria:
        named = await maria.put("/api/aliases/walt", json={"alias": "  Walter   W "})
        assert named.status_code == 200
        assert {"login": "walt", "alias": "Walter W", "own": False} in named.json()[
            "aliases"
        ]
        assert named.json()["can_name_others"]
    async with _as(app, "bob") as bob:
        assert (
            await bob.put("/api/aliases/walt", json={"alias": "Mallory"})
        ).status_code == 403
        assert (
            await bob.put("/api/me/alias", json={"alias": "Walter W"})
        ).status_code == 409, "walt goes by that name"
        assert (
            await bob.put("/api/me/alias", json={"alias": "walt"})
        ).status_code == 409, "a name may not be someone else's login"
        assert (await bob.put("/api/me/alias", json={"alias": "@x"})).status_code == 422
        mine = await bob.put("/api/me/alias", json={"alias": "鲍勃"})
        assert {"login": "bob", "alias": "鲍勃", "own": True} in mine.json()["aliases"]
        assert not mine.json()["can_name_others"]
    async with _as(app, "walt") as walt:
        own = await walt.put("/api/me/alias", json={"alias": "Walt White"})
        assert {"login": "walt", "alias": "Walt White", "own": True} in own.json()[
            "aliases"
        ]
    async with _as(app, "maria") as maria:
        assert (
            await maria.put("/api/aliases/walt", json={"alias": "Walter"})
        ).status_code == 409, "walt chose his own"
        seen = (await maria.get("/api/aliases")).json()["aliases"]
        assert {a["login"]: a["alias"] for a in seen} == {
            "bob": "鲍勃",
            "walt": "Walt White",
        }
    async with _as(app, "bob") as bob:
        cleared = await bob.put("/api/me/alias", json={"alias": None})
        assert [a["login"] for a in cleared.json()["aliases"]] == ["walt"]


async def test_the_pr_list_ranks_an_overridden_p0_above_higher_scored_p1s(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        before = [r["number"] for r in (await maria.get("/api/prs")).json()]
        last = before[-1]
        pinned = await maria.post(
            f"/api/prs/{REPO}/{last}/override",
            json={"tier": "P0", "reason": "on the team's list"},
        )
        assert pinned.status_code == 200
        rows = (await maria.get("/api/prs")).json()
        assert rows[0]["number"] == last and rows[0]["tier"] == "P0"
        assert "_rank" not in rows[0]
