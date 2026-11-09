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
from auto_maintainer.playbooks.review_revise import FollowupRevision, apply_revision
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import make_pr
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
        assert turn.prompt.index("Why is F1 a finding?") < turn.prompt.index(
            "<untrusted-data"
        ), "the reviewer's words come first, outside the untrusted-data fences"
        assert "inline comment on tests/test_loader.py:20" in turn.prompt, (
            "the agent sees the draft as it would be posted"
        )

    async with _as(app, "walt") as walt:
        shared = (await walt.get(f"/api/jobs/{job['id']}/talk")).json()
        assert [m["author"] for m in shared["messages"] if m["role"] == "user"] == [
            "maria"
        ]
        refused = await walt.post(
            f"/api/jobs/{job['id']}/talk",
            json={"text": "test_one_item covers it; please drop F1."},
        )
        assert refused.status_code == 403, (
            "a turn can revise the draft, so only its asker and the maintainers talk"
        )

    # maria asked for the review (and maintains acme/widget in the harness).
    async with _as(app, "maria") as maria:
        dropped = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "test_one_item covers it; please drop F1."},
            )
        ).json()
        assert dropped["proposals"] == [draft]
        steps = (
            await maria.get(f"/api/jobs/{job['id']}/talk/steps?run={dropped['run_id']}")
        ).json()
        assert [s["kind"] for s in steps["steps"]][-1] == "done"
    record = await h.store.get_action(draft)
    assert record is not None
    payload = ReviewPostPayload.model_validate(record.action.payload)
    assert payload.comments == [] and payload.recommendation == "Ready"
    assert (payload.result or {})["findings"] == [], "a fix reads the revised findings"
    assert (payload.result or {})["quality"]["score"] == 88, (
        "a revision that does not judge the quality again keeps it"
    )
    proposal = await h.store.proposal_for_action(draft)
    assert proposal is not None and proposal.edited and proposal.state == "pending"
    edits = [s for s in await h.store.list_signals() if s.kind == "draft_edited"]
    assert edits and edits[-1].source == "maria", "the edit is the reviewer's"
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
    answer = FollowupRevision(
        reply="ok",
        followup=FollowupResult(
            statuses=[FindingStatus(id="F1", status="resolved", title="t")],
            recommendation="Ready",
        ),
    )
    profile = RepoProfile(repo=REPO)
    pr = make_pr(REPO, 42, sha="a" * 40)
    out = apply_revision(answer, payload, profile, pr, "a" * 40, own_format=False)
    assert out is not None and out["result"]["previous_sha"] == "b" * 40
    assert "aaaaaaa" in out["body"] and out["recommendation"] == "Ready"


async def test_a_posted_review_is_revised_by_an_edit_of_the_posted_text(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        job = await _reviewed(h, maria, 42)
        talk = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        draft = talk["draft"]
        proposal = await h.store.proposal_for_action(draft)
        assert proposal is not None
        await maria.post(f"/api/proposals/{proposal.id}/approve", json={})
        await h.settle()
        posted = await h.store.get_action(draft)
        assert posted is not None and posted.status == "executed"
        review_id = posted.external_ref["review_id"]

        said = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "test_one_item covers it; please drop F1."},
            )
        ).json()
        [edit_id] = said["proposals"]
        assert edit_id != draft and "stay as they are" in said["text"]
        state = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        assert state["posted"] and state["revision"] == edit_id
        edit = await h.store.get_action(edit_id)
        assert edit is not None and edit.action.kind == "review.update"
        assert edit.action.payload["target_id"] == review_id
        assert edit.action.payload["revises"] == draft
        assert edit.action.payload["author"] is None
        assert edit.action.head_sha == posted.action.head_sha, (
            "the edited review still covers the commit it reviewed"
        )

        again = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk", json={"text": "Why is F1 a finding?"}
            )
        ).json()
        assert again["proposals"] == [], "a turn that only answers drafts nothing"
        assert edit.action.payload["body"].strip()[:80] in h.runner.tasks[-1].prompt, (
            "the next turn reads the review as the waiting edit has it"
        )

        edit_proposal = await h.store.proposal_for_action(edit_id)
        assert edit_proposal is not None
        refused = await maria.post(
            f"/api/proposals/{edit_proposal.id}/approve", json={"as_me": True}
        )
        assert refused.status_code == 409, "the service's review is edited as it"
        await maria.post(f"/api/proposals/{edit_proposal.id}/approve", json={})
        await h.settle()
    [update] = h.forge.writes_of("update_review")
    assert update["review_id"] == review_id and "F1" not in update["body"]
    revised = await h.store.get_action(draft)
    assert revised is not None
    assert (revised.action.payload["result"] or {})["findings"] == [], (
        "follow-ups and scores read the revised review"
    )
    [record] = await h.store.list_review_records(REPO, 42)
    assert (
        record.result["findings"] == []
        and record.result["body"] == (revised.action.payload["body"])
    )


async def test_a_review_posted_as_a_maintainer_is_edited_only_by_them(demo) -> None:
    from auto_maintainer.actuator.pipeline import ApprovalError
    from auto_maintainer.domain.actions import (
        ActionKind,
        ReviewUpdatePayload,
        make_action,
    )

    h, _app = demo
    action = make_action(
        ActionKind.REVIEW_UPDATE,
        REPO,
        ReviewUpdatePayload(
            target="review", target_id=7, body="New text.", author="maria"
        ),
        number=42,
        head_sha="a" * 40,
        discriminator="talk:test",
        draft_for="maria",
    )
    await h.service.pipeline.submit(action, force_propose=True)
    proposal = await h.store.proposal_for_action(action.id)
    assert proposal is not None
    with pytest.raises(ApprovalError):
        await h.service.pipeline.approve(proposal.id, "walt", post_as="walt")
    with pytest.raises(ApprovalError):
        await h.service.pipeline.approve(proposal.id, "maria")
    with pytest.raises(ApprovalError, match="as themselves"):
        # An edit cannot hand the review to someone else.
        await h.service.pipeline.approve(
            proposal.id,
            "walt",
            {**action.payload, "author": "walt"},
            post_as="walt",
        )
    approved = await h.service.pipeline.approve(proposal.id, "maria", post_as="maria")
    assert approved.state == "approved"
    record = await h.store.get_action(action.id)
    assert record is not None and record.post_as == "maria"


async def test_a_message_reaches_the_agent_while_it_reviews(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        job = await _reviewed(h, maria, 42)
        h.runner.listening_jobs[job["id"]] = 1
        state = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        assert state["listening"]
        sent = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "Also check the docs for the new flag."},
            )
        ).json()
        assert sent["while_running"] and sent["author"] == "maria"
        [(job_id, by, text)] = h.runner.told
        assert (job_id, by) == (job["id"], "maria")
        assert "Also check the docs for the new flag." in text
        assert "never post, push, or run the PR's code" in text
        h.runner.listening_jobs[job["id"]] = 0
        state = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        assert not state["listening"]
        assert [m["text"] for m in state["messages"]] == [
            "Also check the docs for the new flag."
        ]
    steps = {str(t.kind): t.messages for t in h.runner.tasks if t.job_id == job["id"]}
    assert steps["review"], "the agent writing the review takes messages"
    assert not any(v for k, v in steps.items() if k != "review"), steps


async def test_asking_for_a_run_in_the_conversation_starts_a_reproduction(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        job = await _reviewed(h, maria, 42)
        h.runner.on(
            "review",
            lambda task: {
                "reply": "I asked for a run on the test machine.",
                "review": None,
                "reproduce": "Load a batch of one item on the base and the head.",
            },
        )
        said = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk", json={"text": "Please reproduce it."}
            )
        ).json()
        assert (
            len(said["jobs"]) == 1
            and "Started a reproduction on gpu-01" in said["text"]
        )
        await h.settle()
        work = (
            await maria.get(f"/api/work?repo={REPO}&number=42&playbook=reproduce")
        ).json()
    assert work, "the reproduction runs for the reviewer who asked"
    [task] = [t for t in h.runner.tasks if t.kind == "reproduce"][-1:]
    assert "Load a batch of one item on the base and the head." in task.prompt
    assert task.requested_by == "maria"
