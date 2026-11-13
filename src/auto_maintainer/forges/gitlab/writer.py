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

"""``ForgeWriter`` for GitLab (design 5.16).

A review is a note with the summary and one diff discussion per inline comment,
placed on the diff version of the commit it reviewed; a comment GitLab will not
place on that version (a line outside the diff) is added to the summary instead.
GitLab has no verdict that blocks a merge through this API, so a request for
changes posts as a comment (``ForgeCapabilities.blocking_verdict``). Suggestions are
written in GitLab's form, ``suggestion:-<above>+<below>``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from ...domain.forges import check_repo, parse_person
from ...domain.ports import ReviewCommentSpec
from ...domain.snapshot import CommentRef
from ...domain.visibility import PublicText
from .client import GitLabClient, GitLabError, project_id

_SUGGESTION = re.compile(r"^```suggestion\s*$", re.MULTILINE)


def _public(value: object, repo: str, field: str) -> str:
    """The text of ``value`` if it is ``PublicText`` built for ``repo``."""
    if not isinstance(value, PublicText):
        raise TypeError(
            f"{field} must be PublicText from the leak guard, got {type(value).__name__}"
        )
    if check_repo(value.repo) != check_repo(repo):
        raise ValueError(f"{field} was checked for {value.repo}, not {repo}")
    return value.text


def gitlab_suggestions(text: str, above: int = 0) -> str:
    """GitHub's suggestion blocks in GitLab's form: replace this line and the
    ``above`` lines before it."""
    return _SUGGESTION.sub(f"```suggestion:-{above}+0", text)


class GitLabWriter:
    """Implements ``ForgeWriter`` for one GitLab forge.

    Args:
        client: Its API client; its token decides who the writes come from.
        web_url: The forge's web address, for links to what was written.
        person: The login whose own token the client holds; only such a writer
            approves a merge request. ``None`` for the service's own account.
    """

    def __init__(
        self, client: GitLabClient, web_url: str, *, person: str | None = None
    ) -> None:
        self._client = client
        self._web = web_url.rstrip("/")
        self._person = person

    def _mr(self, repo: str, number: int) -> str:
        return f"/projects/{project_id(repo)}/merge_requests/{number}"

    def _note_url(self, repo: str, number: int, note_id: int) -> str:
        path = repo.partition(":")[2] if ":" in repo else repo
        return f"{self._web}/{path}/-/merge_requests/{number}#note_{note_id}"

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            await self._client.put(
                self._mr(repo, number), {"add_labels": ",".join(labels)}
            )

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        await self._client.put(self._mr(repo, number), {"remove_labels": label})

    async def create_comment(
        self, repo: str, number: int, body: PublicText
    ) -> CommentRef:
        note = await self._client.post(
            f"{self._mr(repo, number)}/notes", {"body": _public(body, repo, "body")}
        )
        return CommentRef(
            id=int(note["id"]), url=self._note_url(repo, number, note["id"])
        )

    async def update_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> None:
        await self._client.put(
            f"{self._mr(repo, number)}/notes/{comment_id}",
            {"body": _public(body, repo, "body")},
        )

    async def delete_comment(self, repo: str, number: int, comment_id: int) -> None:
        await self._client.delete(
            f"{self._mr(repo, number)}/notes/{comment_id}", allow=(404,)
        )

    async def _version(self, repo: str, number: int, commit_sha: str) -> dict[str, str]:
        """The base, start, and head SHAs of the diff version of ``commit_sha``."""
        versions = await self._client.get(f"{self._mr(repo, number)}/versions")
        for v in versions or []:
            if str(v.get("head_commit_sha", "")).startswith(commit_sha[:12]):
                return {
                    "base_sha": v["base_commit_sha"],
                    "start_sha": v["start_commit_sha"],
                    "head_sha": v["head_commit_sha"],
                }
        mr = await self._client.get(self._mr(repo, number))
        return dict(mr.get("diff_refs") or {})

    async def create_review(
        self,
        repo: str,
        number: int,
        commit_sha: str,
        body: PublicText,
        event: Literal["COMMENT", "REQUEST_CHANGES"],
        comments: list[ReviewCommentSpec],
    ) -> CommentRef:
        summary = _public(body, repo, "body")
        refs = await self._version(repo, number, commit_sha) if comments else {}
        unplaced: list[str] = []
        for c in comments:
            text = gitlab_suggestions(
                _public(c.body, repo, f"comment on {c.path}:{c.line}"),
                above=max(0, c.line - c.start_line) if c.start_line else 0,
            )
            position: dict[str, Any] = {
                "position_type": "text",
                **refs,
                "new_path": c.path,
                "old_path": c.path,
            }
            position["old_line" if c.side == "LEFT" else "new_line"] = c.line
            try:
                await self._client.post(
                    f"{self._mr(repo, number)}/discussions",
                    {"body": text, "position": position},
                )
            except GitLabError as e:
                if e.status != 400:
                    raise
                unplaced.append(f"**{c.path}:{c.line}**\n\n{text}")
        if unplaced:
            summary = "\n\n".join([summary, *unplaced])
        note = await self._client.post(
            f"{self._mr(repo, number)}/notes", {"body": summary}
        )
        return CommentRef(
            id=int(note["id"]), url=self._note_url(repo, number, note["id"])
        )

    async def update_review(
        self, repo: str, number: int, review_id: int, body: PublicText
    ) -> None:
        await self.update_comment(repo, number, review_id, body)

    async def approve_workflow_run(self, repo: str, run_id: int) -> None:
        raise NotImplementedError(
            "GitLab holds no pipeline for a maintainer's approval through this API"
        )

    async def approve_pr(
        self, repo: str, number: int, commit_sha: str, body: PublicText
    ) -> CommentRef:
        if self._person is None:
            raise PermissionError("the service's own account never approves a change")
        text = _public(body, repo, "body")
        await self._client.post(
            f"{self._mr(repo, number)}/approve", {"sha": commit_sha}
        )
        if text.strip():
            return await self.create_comment(repo, number, body)
        return CommentRef(id=0, url=f"{self._web}")

    async def resolve_review_thread(
        self, repo: str, number: int, thread_id: str
    ) -> None:
        await self._client.put(
            f"{self._mr(repo, number)}/discussions/{thread_id}",
            json={"resolved": True},
        )

    async def reply_to_review_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> CommentRef:
        discussions = await self._client.paginate(
            f"{self._mr(repo, number)}/discussions"
        )
        found = next(
            (
                d
                for d in discussions
                if any(int(n["id"]) == comment_id for n in d.get("notes") or [])
            ),
            None,
        )
        if found is None:
            raise GitLabError(404, f"no discussion holds note {comment_id}")
        note = await self._client.post(
            f"{self._mr(repo, number)}/discussions/{found['id']}/notes",
            {"body": _public(body, repo, "body")},
        )
        return CommentRef(
            id=int(note["id"]), url=self._note_url(repo, number, note["id"])
        )

    async def _user_id(self, login: str) -> int | None:
        users = await self._client.get(
            "/users", params={"username": parse_person(login).login}
        )
        return int(users[0]["id"]) if users else None

    async def request_reviewers(
        self, repo: str, number: int, logins: list[str]
    ) -> None:
        mr = await self._client.get(self._mr(repo, number))
        ids = [int(r["id"]) for r in mr.get("reviewers") or []]
        for login in logins:
            user_id = await self._user_id(login)
            if user_id is not None and user_id not in ids:
                ids.append(user_id)
        await self._client.put(self._mr(repo, number), {"reviewer_ids": ids})

    async def update_branch(
        self, repo: str, number: int, expected_head_sha: str
    ) -> None:
        """Rebase the merge request onto its target, if its head is still the one
        the decision saw."""
        mr = await self._client.get(self._mr(repo, number))
        if not str(mr.get("sha", "")).startswith(expected_head_sha[:12]):
            raise GitLabError(409, f"the head moved past {expected_head_sha[:7]}")
        await self._client.put(f"{self._mr(repo, number)}/rebase")

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
        message = (
            f"{_public(subject, repo, 'subject')}\n\n{_public(body, repo, 'body')}"
        )
        payload: dict[str, Any] = {"sha": sha, "squash": method == "squash"}
        if method == "squash":
            payload["squash_commit_message"] = message
        elif method == "merge":
            payload["merge_commit_message"] = message
        data = await self._client.put(f"{self._mr(repo, number)}/merge", payload)
        if not data or data.get("state") != "merged":
            raise GitLabError(
                200,
                f"merging {repo}!{number} did not complete: {(data or {}).get('merge_error')}",
            )
        return str(
            data.get("merge_commit_sha")
            or data.get("squash_commit_sha")
            or data.get("sha")
        )

    async def enqueue_merge(self, repo: str, number: int, sha: str) -> None:
        raise NotImplementedError("GitLab merge trains are not supported")

    async def update_pr(
        self, repo: str, number: int, title: PublicText, body: PublicText
    ) -> None:
        await self._client.put(
            self._mr(repo, number),
            {
                "title": _public(title, repo, "title"),
                "description": _public(body, repo, "body"),
            },
        )

    async def convert_to_draft(self, repo: str, number: int) -> None:
        mr = await self._client.get(self._mr(repo, number))
        title = str(mr.get("title") or "")
        if not mr.get("draft"):
            await self._client.put(self._mr(repo, number), {"title": f"Draft: {title}"})

    async def close(self, repo: str, number: int) -> None:
        await self._client.put(self._mr(repo, number), {"state_event": "close"})

    async def create_service_issue(
        self, repo: str, title: str, body: str
    ) -> CommentRef:
        issue = await self._client.post(
            f"/projects/{project_id(repo)}/issues",
            {"title": title, "description": body},
        )
        return CommentRef(id=int(issue["iid"]), url=str(issue.get("web_url") or ""))

    async def _open(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> CommentRef:
        mr = await self._client.post(
            f"/projects/{project_id(repo)}/merge_requests",
            {
                "source_branch": head.rpartition(":")[2],
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        return CommentRef(id=int(mr["iid"]), url=str(mr.get("web_url") or ""))

    async def create_service_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> CommentRef:
        return await self._open(repo, head, base, title, body)

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
        checked = _public(title, repo, "title")
        if draft and not checked.lower().startswith("draft:"):
            checked = f"Draft: {checked}"
        return await self._open(repo, head, base, checked, _public(body, repo, "body"))

    async def merge_service_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> str:
        data = await self._client.put(
            f"{self._mr(repo, number)}/merge", {"squash": method == "squash"}
        )
        return str(
            (data or {}).get("merge_commit_sha") or (data or {}).get("sha") or ""
        )
