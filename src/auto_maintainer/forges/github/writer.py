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

"""``ForgeWriter`` for GitHub.

Methods that post text to a target repository take ``PublicText`` built for that
repository and refuse anything else. Removing a label or deleting a comment that is
already gone succeeds, so retries are harmless.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from ...domain.ports import ReviewCommentSpec
from ...domain.snapshot import CommentRef
from ...domain.visibility import PublicText
from .client import GitHubClient, GitHubError

_DRAFT_MUTATION = """
mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } } }
"""
_RESOLVE_MUTATION = """
mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }
"""
_PR_NODE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { id } }
}
"""


def _public(value: object, repo: str, field: str) -> str:
    """Return the text of ``value`` if it is ``PublicText`` built for ``repo``."""
    if not isinstance(value, PublicText):
        raise TypeError(
            f"{field} must be PublicText from the leak guard, got {type(value).__name__}"
        )
    if value.repo != repo:
        raise ValueError(f"{field} was checked for {value.repo}, not {repo}")
    return value.text


class GitHubWriter:
    """``ForgeWriter`` backed by the GitHub REST and GraphQL APIs.

    Args:
        client: The API client. Its token provider decides who the writes come from.
        person: The login whose own token the client holds; only such a writer
            approves a PR. ``None`` for the service's own account.
    """

    def __init__(self, client: GitHubClient, *, person: str | None = None) -> None:
        self._client = client
        self._person = person

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            await self._client.post(
                f"/repos/{repo}/issues/{number}/labels",
                repo=repo,
                json={"labels": labels},
            )

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        await self._client.delete(
            f"/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}",
            repo=repo,
            allow=(404,),
        )

    async def create_comment(
        self, repo: str, number: int, body: PublicText
    ) -> CommentRef:
        data = await self._client.post(
            f"/repos/{repo}/issues/{number}/comments",
            repo=repo,
            json={"body": _public(body, repo, "body")},
        )
        return CommentRef(id=int(data["id"]), url=data.get("html_url") or "")

    async def update_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> None:
        await self._client.patch(
            f"/repos/{repo}/issues/comments/{comment_id}",
            repo=repo,
            json={"body": _public(body, repo, "body")},
        )

    async def delete_comment(self, repo: str, number: int, comment_id: int) -> None:
        await self._client.delete(
            f"/repos/{repo}/issues/comments/{comment_id}", repo=repo, allow=(404,)
        )

    async def create_review(
        self,
        repo: str,
        number: int,
        commit_sha: str,
        body: PublicText,
        event: Literal["COMMENT", "REQUEST_CHANGES"],
        comments: list[ReviewCommentSpec],
    ) -> CommentRef:
        if event not in ("COMMENT", "REQUEST_CHANGES"):
            raise ValueError(
                f"review event must be COMMENT or REQUEST_CHANGES, got {event!r}"
            )
        payload_comments: list[dict[str, Any]] = []
        for c in comments:
            item: dict[str, Any] = {
                "path": c.path,
                "line": c.line,
                "side": c.side,
                "body": _public(c.body, repo, f"comment on {c.path}:{c.line}"),
            }
            if c.start_line is not None and c.start_line < c.line:
                item["start_line"] = c.start_line
                item["start_side"] = c.side
            payload_comments.append(item)
        data = await self._client.post(
            f"/repos/{repo}/pulls/{number}/reviews",
            repo=repo,
            json={
                "commit_id": commit_sha,
                "body": _public(body, repo, "body"),
                "event": event,
                "comments": payload_comments,
            },
        )
        return CommentRef(id=int(data["id"]), url=data.get("html_url") or "")

    async def approve_workflow_run(self, repo: str, run_id: int) -> None:
        await self._client.post(
            f"/repos/{repo}/actions/runs/{run_id}/approve", repo=repo
        )

    async def approve_pr(
        self, repo: str, number: int, commit_sha: str, body: PublicText
    ) -> CommentRef:
        if self._person is None:
            raise PermissionError("the service's own account never approves a PR")
        data = await self._client.post(
            f"/repos/{repo}/pulls/{number}/reviews",
            repo=repo,
            json={
                "commit_id": commit_sha,
                "body": _public(body, repo, "body"),
                "event": "APPROVE",
            },
        )
        return CommentRef(id=int(data["id"]), url=data.get("html_url") or "")

    async def update_review(
        self, repo: str, number: int, review_id: int, body: PublicText
    ) -> None:
        await self._client.put(
            f"/repos/{repo}/pulls/{number}/reviews/{review_id}",
            repo=repo,
            json={"body": _public(body, repo, "body")},
        )

    async def reply_to_review_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> CommentRef:
        data = await self._client.post(
            f"/repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
            repo=repo,
            json={"body": _public(body, repo, "body")},
        )
        return CommentRef(id=int(data["id"]), url=data.get("html_url") or "")

    async def resolve_review_thread(
        self, repo: str, number: int, thread_id: str
    ) -> None:
        await self._client.graphql(_RESOLVE_MUTATION, {"id": thread_id}, repo=repo)

    async def request_reviewers(
        self, repo: str, number: int, logins: list[str]
    ) -> None:
        if logins:
            await self._client.post(
                f"/repos/{repo}/pulls/{number}/requested_reviewers",
                repo=repo,
                json={"reviewers": logins},
            )

    async def update_branch(
        self, repo: str, number: int, expected_head_sha: str
    ) -> None:
        """Merge the base branch into the PR's branch. A sign-in through an OAuth
        app without the ``workflow`` scope (a person's dashboard sign-in) cannot do
        this when the base's new commits change workflow files; the error then says
        so and what to do instead, with GitHub's own message."""
        try:
            await self._client.put(
                f"/repos/{repo}/pulls/{number}/update-branch",
                repo=repo,
                json={"expected_head_sha": expected_head_sha},
            )
        except GitHubError as e:
            if e.status == 403 and "workflow" in str(e) and "scope" in str(e):
                raise GitHubError(
                    403,
                    "the base branch's new commits change workflow files, and the "
                    "GitHub sign-in this service holds may not change workflow "
                    "files (it lacks the workflow scope). Press Update branch on "
                    "the PR's page on GitHub, or merge the base branch locally and "
                    f"push. GitHub said: {str(e).split(': ', 1)[-1]}",
                    e.body,
                ) from e
            raise

    async def merge(
        self,
        repo: str,
        number: int,
        sha: str,
        method: str,
        subject: PublicText,
        body: PublicText,
    ) -> str:
        if method not in ("squash", "merge", "rebase"):
            raise ValueError(
                f"merge method must be squash, merge, or rebase, got {method!r}"
            )
        payload: dict[str, Any] = {"sha": sha, "merge_method": method}
        if method != "rebase":
            payload["commit_title"] = _public(subject, repo, "subject")
            payload["commit_message"] = _public(body, repo, "body")
        data = await self._client.put(
            f"/repos/{repo}/pulls/{number}/merge", repo=repo, json=payload
        )
        if not data or not data.get("merged"):
            raise GitHubError(
                200,
                f"merging {repo}#{number} did not complete: {(data or {}).get('message')}",
                data,
            )
        return data["sha"]

    async def update_pr(
        self, repo: str, number: int, title: PublicText, body: PublicText
    ) -> None:
        await self._client.patch(
            f"/repos/{repo}/pulls/{number}",
            repo=repo,
            json={
                "title": _public(title, repo, "title"),
                "body": _public(body, repo, "body"),
            },
        )

    async def convert_to_draft(self, repo: str, number: int) -> None:
        owner, _, name = repo.partition("/")
        data = await self._client.graphql(
            _PR_NODE_QUERY, {"owner": owner, "name": name, "number": number}, repo=repo
        )
        node_id = data["repository"]["pullRequest"]["id"]
        await self._client.graphql(_DRAFT_MUTATION, {"id": node_id}, repo=repo)

    async def close(self, repo: str, number: int) -> None:
        await self._client.patch(
            f"/repos/{repo}/pulls/{number}", repo=repo, json={"state": "closed"}
        )

    async def create_service_issue(
        self, repo: str, title: str, body: str
    ) -> CommentRef:
        data = await self._client.post(
            f"/repos/{repo}/issues", repo=repo, json={"title": title, "body": body}
        )
        return CommentRef(id=int(data["number"]), url=data.get("html_url") or "")

    async def create_service_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> CommentRef:
        data = await self._client.post(
            f"/repos/{repo}/pulls",
            repo=repo,
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return CommentRef(id=int(data["number"]), url=data.get("html_url") or "")

    async def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: PublicText,
        body: PublicText,
        *,
        draft: bool = False,
    ) -> CommentRef:
        data = await self._client.post(
            f"/repos/{repo}/pulls",
            repo=repo,
            json={
                "title": str(title),
                "head": head,
                "base": base,
                "body": str(body),
                **({"draft": True} if draft else {}),
            },
        )
        return CommentRef(id=int(data["number"]), url=data.get("html_url") or "")

    async def merge_service_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> str:
        data = await self._client.put(
            f"/repos/{repo}/pulls/{number}/merge",
            repo=repo,
            json={"merge_method": method},
        )
        if not data or not data.get("merged"):
            raise GitHubError(200, f"merging {repo}#{number} did not complete", data)
        return data["sha"]
