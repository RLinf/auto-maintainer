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

"""GitHubWriter: PublicText enforcement and request shapes."""

from __future__ import annotations

import json

import httpx
import pytest
from gh_testutil import public

from auto_maintainer.domain.ports import ReviewCommentSpec
from auto_maintainer.forges.github import GitHubError, GitHubWriter


def _json(route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


async def test_rejects_plain_strings(client):
    writer = GitHubWriter(client)
    with pytest.raises(TypeError):
        await writer.create_comment("acme/widget", 1, "hello")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await writer.merge("acme/widget", 1, "s", "squash", "subject", public("b"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await writer.create_comment("acme/widget", 1, public("hi", repo="other/repo"))


async def test_comment_create_update_delete(client, router):
    create = router.post("/repos/acme/widget/issues/3/comments").respond(
        201, json={"id": 11, "html_url": "https://github.com/acme/widget/pull/3#c11"}
    )
    update = router.patch("/repos/acme/widget/issues/comments/11").respond(200, json={})
    delete = router.delete("/repos/acme/widget/issues/comments/11").respond(404)
    writer = GitHubWriter(client)
    ref = await writer.create_comment("acme/widget", 3, public("hi"))
    assert ref.id == 11 and _json(create) == {"body": "hi"}
    await writer.update_comment("acme/widget", 42, 11, public("edited"))
    assert _json(update) == {"body": "edited"}
    await writer.delete_comment("acme/widget", 42, 11)
    assert delete.called


async def test_labels(client, router):
    add = router.post("/repos/acme/widget/issues/3/labels").respond(200, json=[])
    remove = router.delete("/repos/acme/widget/issues/3/labels/needs%20review").respond(
        404
    )
    writer = GitHubWriter(client)
    await writer.add_labels("acme/widget", 3, ["bug", "needs review"])
    await writer.add_labels("acme/widget", 3, [])
    await writer.remove_label("acme/widget", 3, "needs review")
    assert add.call_count == 1 and _json(add) == {"labels": ["bug", "needs review"]}
    assert remove.called


async def test_create_review_payload(client, router):
    route = router.post("/repos/acme/widget/pulls/3/reviews").respond(
        200, json={"id": 77, "html_url": "u"}
    )
    writer = GitHubWriter(client)
    ref = await writer.create_review(
        "acme/widget",
        3,
        "abc",
        public("main"),
        "COMMENT",
        [
            ReviewCommentSpec(path="a.py", line=10, body=public("one")),
            ReviewCommentSpec(
                path="b.py", line=20, start_line=15, body=public("range")
            ),
        ],
    )
    assert ref.id == 77
    assert _json(route) == {
        "commit_id": "abc",
        "body": "main",
        "event": "COMMENT",
        "comments": [
            {"path": "a.py", "line": 10, "side": "RIGHT", "body": "one"},
            {
                "path": "b.py",
                "line": 20,
                "side": "RIGHT",
                "body": "range",
                "start_line": 15,
                "start_side": "RIGHT",
            },
        ],
    }
    with pytest.raises(ValueError):
        await writer.create_review("acme/widget", 3, "abc", public("x"), "APPROVE", [])  # type: ignore[arg-type]


async def test_merge_payload_and_result(client, router):
    route = router.put("/repos/acme/widget/pulls/3/merge").respond(
        200, json={"merged": True, "sha": "m123"}
    )
    sha = await GitHubWriter(client).merge(
        "acme/widget",
        3,
        "head1",
        "squash",
        public("fix: thing (#3)"),
        public("* fix\n\nSigned-off-by: A <a@x>"),
    )
    assert sha == "m123"
    assert _json(route) == {
        "sha": "head1",
        "merge_method": "squash",
        "commit_title": "fix: thing (#3)",
        "commit_message": "* fix\n\nSigned-off-by: A <a@x>",
    }


async def test_merge_not_merged_raises(client, router):
    router.put("/repos/acme/widget/pulls/3/merge").respond(
        200, json={"merged": False, "message": "nope"}
    )
    with pytest.raises(GitHubError):
        await GitHubWriter(client).merge(
            "acme/widget", 3, "h", "merge", public("s"), public("b")
        )


async def test_review_update_reply_reviewers_branch(client, router):
    upd = router.put("/repos/acme/widget/pulls/3/reviews/77").respond(200, json={})
    reply = router.post("/repos/acme/widget/pulls/3/comments/8001/replies").respond(
        201, json={"id": 9}
    )
    reviewers = router.post("/repos/acme/widget/pulls/3/requested_reviewers").respond(
        201, json={}
    )
    branch = router.put("/repos/acme/widget/pulls/3/update-branch").respond(
        202, json={}
    )
    close = router.patch("/repos/acme/widget/pulls/3").respond(200, json={})
    writer = GitHubWriter(client)
    await writer.update_review("acme/widget", 3, 77, public("v2"))
    assert (
        await writer.reply_to_review_comment(
            "acme/widget", 3, 8001, public("Resolved in abc1234.")
        )
    ).id == 9
    await writer.request_reviewers("acme/widget", 3, ["carol"])
    await writer.update_branch("acme/widget", 3, "head1")
    await writer.close("acme/widget", 3)
    assert _json(upd) == {"body": "v2"} and _json(reviewers) == {"reviewers": ["carol"]}
    assert _json(branch) == {"expected_head_sha": "head1"} and _json(close) == {
        "state": "closed"
    }
    assert reply.called


async def test_update_branch_says_why_a_sign_in_without_workflow_scope_fails(
    client, router
):
    refusal = (
        "refusing to allow an OAuth App to create or update workflow "
        "`.github/workflows/e2e.yml` without `workflow` scope"
    )
    router.put("/repos/acme/widget/pulls/3/update-branch").respond(
        403, json={"message": refusal}
    )
    with pytest.raises(GitHubError) as raised:
        await GitHubWriter(client).update_branch("acme/widget", 3, "head1")
    said = str(raised.value)
    assert raised.value.status == 403
    assert "change workflow files" in said and "Update branch" in said
    assert refusal in said, "GitHub's own message stays in the error"


async def test_update_branch_passes_other_refusals_through(client, router):
    router.put("/repos/acme/widget/pulls/3/update-branch").respond(
        403, json={"message": "Resource not accessible by integration"}
    )
    with pytest.raises(GitHubError) as raised:
        await GitHubWriter(client).update_branch("acme/widget", 3, "head1")
    assert "workflow" not in str(raised.value)


async def test_convert_to_draft_uses_node_id(client, router):
    route = router.post("/graphql").mock(
        side_effect=[
            httpx.Response(
                200, json={"data": {"repository": {"pullRequest": {"id": "PR_node"}}}}
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}
                    }
                },
            ),
        ]
    )
    await GitHubWriter(client).convert_to_draft("acme/widget", 3)
    assert _json(route, 1)["variables"] == {"id": "PR_node"}


async def test_service_issue_and_pr(client, router):
    router.post("/repos/acme/memory/issues").respond(
        201, json={"number": 4, "html_url": "i"}
    )
    router.post("/repos/acme/memory/pulls").respond(
        201, json={"number": 5, "html_url": "p"}
    )
    router.put("/repos/acme/memory/pulls/5/merge").respond(
        200, json={"merged": True, "sha": "x"}
    )
    writer = GitHubWriter(client)
    assert (await writer.create_service_issue("acme/memory", "t", "b")).id == 4
    assert (
        await writer.create_service_pr("acme/memory", "am/mem/1", "main", "t", "b")
    ).id == 5
    assert await writer.merge_service_pr("acme/memory", 5) == "x"
