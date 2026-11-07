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

"""Reviewers talking with the agent about its review: answers, revisions of the
pending draft, lessons, who may take part, and drafts already decided."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.actions import ReviewPostPayload
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import RepoProfile
from auto_maintainer.domain.review import FindingStatus, FollowupResult
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.web.app import create_app
from auto_maintainer.web.review_talk import FollowupTalkAnswer, _apply

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


async def _reviewed(h: Any, client: httpx.AsyncClient, number: int) -> dict[str, Any]:
    """Ask for a review of ``number`` and return its job as the work list shows it."""
    await client.post("/api/reviews", json={"prs": [{"repo": REPO, "number": number}]})
    await h.settle()
    work = (await client.get(f"/api/work?repo={REPO}&number={number}")).json()
    [job] = [j for j in work if j["playbook"] == "review"]
    return job


async def test_a_reviewer_talks_the_agent_out_of_a_finding(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        job = await _reviewed(h, maria, 42)
        before = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        assert before["messages"] == [] and before["draft_pending"]
        draft = before["draft"]
        record = await h.store.get_action(draft)
        assert record is not None and "F1" in str(record.action.payload["result"])

        asked = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk", json={"text": "Why is F1 a finding?"}
            )
        ).json()
        assert "boundary at one item" in asked["text"] and asked["proposals"] == []
        turn = h.runner.tasks[-1]
        assert turn.requested_by == "maria" and turn.job_id == asked["run_id"]
        assert "Why is F1 a finding?" in turn.prompt
        assert "inline comment on tests/test_loader.py:20" in turn.prompt, (
            "the agent sees the draft as it would be posted"
        )

    async with _as(app, "walt") as walt:
        shared = (await walt.get(f"/api/jobs/{job['id']}/talk")).json()
        assert [m["author"] for m in shared["messages"] if m["role"] == "user"] == [
            "maria"
        ]
        dropped = (
            await walt.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "test_one_item covers it; please drop F1."},
            )
        ).json()
        assert dropped["proposals"] == [draft]
        steps = (
            await walt.get(f"/api/jobs/{job['id']}/talk/steps?run={dropped['run_id']}")
        ).json()
        assert [s["kind"] for s in steps["steps"]][-1] == "done"
    record = await h.store.get_action(draft)
    assert record is not None
    payload = ReviewPostPayload.model_validate(record.action.payload)
    assert payload.comments == [] and payload.recommendation == "Ready"
    assert (payload.result or {})["findings"] == [], "a fix reads the revised findings"
    proposal = await h.store.proposal_for_action(draft)
    assert proposal is not None and proposal.edited and proposal.state == "pending"
    edits = [s for s in await h.store.list_signals() if s.kind == "draft_edited"]
    assert edits and edits[-1].source == "walt", "the edit is the reviewer's"
    assert "maria: Why is F1 a finding?" in h.runner.tasks[-1].prompt

    async with _as(app, "eve") as eve:
        assert (await eve.get(f"/api/jobs/{job['id']}/talk")).status_code == 403


async def test_a_decided_draft_is_not_revised_and_lessons_are_saved(demo) -> None:
    h, app = demo
    answers = iter(
        [
            {
                "reply": "Noted.",
                "review": {
                    "summary": "s",
                    "findings": [],
                    "recommendation": "Ready",
                },
                "lessons": ["Boundary tests may live in test_loader_one.py."],
            }
        ]
    )
    async with _as(app, "maria") as maria:
        job = await _reviewed(h, maria, 42)
        talk = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        proposal = await h.store.proposal_for_action(talk["draft"])
        assert proposal is not None
        await maria.post(f"/api/proposals/{proposal.id}/reject", json={"reason": "no"})
        h.runner.on("review", lambda task: next(answers))
        said = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "Drop F1 and remember why."},
            )
        ).json()
    assert said["proposals"] == [] and "already decided" in said["text"]
    record = await h.store.get_action(talk["draft"])
    assert record is not None and record.action.payload["comments"], "unchanged"
    assert len(said["changes"]) == 1
    change = await h.store.get_memory_change(said["changes"][0])
    assert change is not None and "test_loader_one.py" in str(change.files)


async def test_only_reviews_have_a_conversation(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post(
            "/api/evaluations", json={"prs": [{"repo": REPO, "number": 42}]}
        )
        await h.settle()
        work = (await maria.get(f"/api/work?repo={REPO}&number=42")).json()
        [scoring] = [j for j in work if j["playbook"] == "evaluate"]
        assert (await maria.get(f"/api/jobs/{scoring['id']}/talk")).status_code == 404


def test_a_revised_follow_up_renders_against_the_commit_it_followed() -> None:
    payload = ReviewPostPayload(
        body="old",
        kind="followup",
        recommendation="Changes requested",
        result={"statuses": [], "new_findings": [], "previous_sha": "b" * 40},
    )
    answer = FollowupTalkAnswer(
        reply="ok",
        followup=FollowupResult(
            statuses=[FindingStatus(id="F1", status="resolved", title="t")],
            recommendation="Ready",
        ),
    )
    profile = RepoProfile(repo=REPO)
    pr = make_pr(REPO, 42, sha="a" * 40)
    out = _apply(answer, payload, profile, pr, "a" * 40, own_format=False)
    assert out is not None and out["result"]["previous_sha"] == "b" * 40
    assert "aaaaaaa" in out["body"] and out["recommendation"] == "Ready"
