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

"""The GitLab adapter against recorded GitLab API answers (design 5.16): a merge
request read as a PR snapshot, and what the service writes there."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from auto_maintainer.domain import forges
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.ports import ReviewCommentSpec
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckStatus,
    Mergeable,
    ReviewEventState,
)
from auto_maintainer.domain.visibility import GUARD_KEY, PublicText
from auto_maintainer.forges.gitlab.ci import GitLabCi
from auto_maintainer.forges.gitlab.client import GitLabClient, GitLabError
from auto_maintainer.forges.gitlab.reader import GitLabReader
from auto_maintainer.forges.gitlab.webhooks import hint, verify_token
from auto_maintainer.forges.gitlab.writer import GitLabWriter

API = "https://gitlab.corp.com/api/v4"
REPO = "corp:grp/sub/proj"
PID = "grp%2Fsub%2Fproj"
HEAD = "c" * 40


@pytest.fixture(autouse=True)
def corp(monkeypatch):
    infos = {
        "github": forges.ForgeInfo(id="github"),
        "corp": forges.ForgeInfo(
            id="corp", kind="gitlab", web_url="https://gitlab.corp.com"
        ),
    }
    monkeypatch.setattr(
        forges, "_forges", forges._Forges("github", frozenset(infos), infos)
    )


class FakeGitLab:
    """Answers GitLab API requests from a table; records what was sent."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], Any] = {}
        self.sent: list[tuple[str, str, Any]] = []

    def on(self, method: str, path: str, answer: Any) -> None:
        self.answers[(method, path)] = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/api/v4")
        body = json.loads(request.content) if request.content else None
        self.sent.append((request.method, path, body))
        answer = self.answers.get((request.method, path))
        if answer is None:
            return httpx.Response(404, json={"message": "404 Not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def client(self) -> GitLabClient:
        async def token() -> str:
            return "glpat-test"

        return GitLabClient(API, token, transport=httpx.MockTransport(self.handle))


def public(text: str, repo: str = REPO) -> PublicText:
    return PublicText(text, repo, _key=GUARD_KEY)


def _merge_request(fake: FakeGitLab) -> None:
    mr = f"/projects/{PID}/merge_requests/7"
    fake.on(
        "GET",
        mr,
        {
            "iid": 7,
            "title": "Speed up the loader",
            "description": "Faster.",
            "author": {"id": 11, "username": "alice"},
            "draft": False,
            "state": "opened",
            "target_branch": "main",
            "source_branch": "fast",
            "sha": HEAD,
            "web_url": "https://gitlab.corp.com/grp/sub/proj/-/merge_requests/7",
            "created_at": "2026-11-01T10:00:00Z",
            "updated_at": "2026-11-02T10:00:00Z",
            "labels": ["perf"],
            "has_conflicts": True,
            "diverged_commits_count": 2,
            "source_project_id": 5,
            "target_project_id": 5,
            "reviewers": [{"id": 12, "username": "bob"}],
            "head_pipeline": {"id": 900, "project_id": 5},
        },
    )
    fake.on(
        "GET",
        f"{mr}/diffs",
        [
            {
                "old_path": "a.py",
                "new_path": "a.py",
                "diff": "@@ -1 +1,2 @@\n-x\n+y\n+z\n",
            },
            {
                "old_path": "b.py",
                "new_path": "b.py",
                "new_file": True,
                "diff": "+new\n",
            },
        ],
    )
    fake.on(
        "GET", f"{mr}/commits", [{"id": HEAD, "message": "fast", "author_email": "a@x"}]
    )
    fake.on(
        "GET",
        f"{mr}/notes",
        [
            {
                "id": 1,
                "system": True,
                "body": "added 1 commit",
                "author": {"username": "alice"},
            },
            {"id": 2, "body": "Looks good", "author": {"username": "carol"}},
            {"id": 3, "body": "nit here", "author": {"username": "bob"}},
        ],
    )
    fake.on(
        "GET",
        f"{mr}/discussions",
        [
            {
                "id": "d1",
                "notes": [
                    {
                        "id": 3,
                        "body": "nit here",
                        "author": {"username": "bob"},
                        "resolvable": True,
                        "resolved": False,
                        "position": {
                            "new_path": "a.py",
                            "new_line": 2,
                            "head_sha": HEAD,
                        },
                    }
                ],
            },
            {
                "id": "d2",
                "notes": [
                    {"id": 2, "body": "Looks good", "author": {"username": "carol"}}
                ],
            },
        ],
    )
    fake.on(
        "GET",
        f"{mr}/approvals",
        {"approved_by": [{"user": {"id": 13, "username": "dave"}}]},
    )
    fake.on(
        "GET",
        f"{mr}/reviewers",
        [{"user": {"id": 12, "username": "bob"}, "state": "requested_changes"}],
    )
    fake.on(
        "GET",
        "/projects/5/pipelines/900/jobs",
        [
            {
                "id": 31,
                "name": "test",
                "stage": "test",
                "status": "success",
                "pipeline": {"id": 900},
            },
            {
                "id": 32,
                "name": "lint",
                "stage": "test",
                "status": "failed",
                "allow_failure": True,
            },
            {"id": 33, "name": "deploy", "stage": "deploy", "status": "manual"},
            {"id": 34, "name": "build", "stage": "build", "status": "pending"},
        ],
    )
    fake.on(
        "GET",
        f"/projects/{PID}/repository/commits/{HEAD}/statuses",
        [
            {
                "id": 41,
                "name": "external-ci",
                "status": "failed",
                "target_url": "https://ci.example/1",
            }
        ],
    )
    fake.on("GET", f"/projects/{PID}/members/all/11", {"access_level": 20})


async def test_a_merge_request_reads_as_a_pr_snapshot() -> None:
    fake = FakeGitLab()
    _merge_request(fake)
    pr = await GitLabReader(fake.client(), "corp").get_pr(REPO, 7)
    assert pr is not None
    assert (pr.repo, pr.number, pr.author, pr.head_repo) == (
        REPO,
        7,
        "corp:alice",
        REPO,
    )
    assert (pr.base_ref, pr.head_ref, pr.head_sha, pr.labels) == (
        "main",
        "fast",
        HEAD,
        ["perf"],
    )
    assert [(f.path, f.additions, f.deletions, f.change_type) for f in pr.files] == [
        ("a.py", 2, 1, "modified"),
        ("b.py", 1, 0, "added"),
    ]
    assert [(c.id, c.author) for c in pr.comments] == [(2, "corp:carol")], (
        "system notes and diff notes are not comments"
    )
    [thread] = pr.threads
    assert (thread.id, thread.path, thread.line, thread.is_resolved) == (
        "d1",
        "a.py",
        2,
        False,
    )
    assert thread.comments[0].author == "corp:bob"
    assert {(r.author, r.state) for r in pr.reviews} == {
        ("corp:dave", ReviewEventState.APPROVED),
        ("corp:bob", ReviewEventState.CHANGES_REQUESTED),
    }
    checks = {c.name: (c.status, c.conclusion) for c in pr.checks}
    assert checks == {
        "test": (CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
        "lint": (CheckStatus.COMPLETED, CheckConclusion.NEUTRAL),
        "deploy": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
        "build": (CheckStatus.QUEUED, None),
        "external-ci": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    }
    assert pr.mergeable == Mergeable.CONFLICTING and pr.behind_base is True
    assert pr.author_association == "CONTRIBUTOR", "a Reporter is not on the team"
    assert pr.requested_reviewers == ["corp:bob"]


async def test_open_merge_requests_are_read_again_only_when_they_change() -> None:
    fake = FakeGitLab()
    _merge_request(fake)
    light = {
        "iid": 7,
        "updated_at": "2026-11-02T10:00:00Z",
        "sha": HEAD,
        "labels": ["perf"],
    }
    fake.on("GET", f"/projects/{PID}/merge_requests", [light])
    # Its CI is still running, so the listing reads it again; once done, it does not.
    reader = GitLabReader(fake.client(), "corp")
    await reader.list_open_prs(REPO)
    first = sum(1 for m, p, _ in fake.sent if p.endswith("/merge_requests/7"))
    fake.on(
        "GET",
        "/projects/5/pipelines/900/jobs",
        [{"id": 31, "name": "test", "status": "success"}],
    )
    await reader.list_open_prs(REPO)
    await reader.list_open_prs(REPO)
    assert (
        sum(1 for m, p, _ in fake.sent if p.endswith("/merge_requests/7")) == first + 1
    )


async def test_roles_map_to_permissions_and_strangers_read_public_projects() -> None:
    fake = FakeGitLab()
    fake.on("GET", "/users", [{"id": 21}])
    fake.on("GET", f"/projects/{PID}/members/all/21", {"access_level": 40})
    reader = GitLabReader(fake.client(), "corp")
    assert await reader.get_permission(REPO, "maria") == Permission.MAINTAIN
    fake.answers.pop(("GET", f"/projects/{PID}/members/all/21"))
    fake.on(
        "GET", f"/projects/{PID}", {"visibility": "public", "default_branch": "main"}
    )
    assert await reader.get_permission(REPO, "maria") == Permission.READ


async def test_a_review_is_a_note_and_a_discussion_on_the_reviewed_version() -> None:
    fake = FakeGitLab()
    mr = f"/projects/{PID}/merge_requests/7"
    fake.on(
        "GET",
        f"{mr}/versions",
        [
            {
                "head_commit_sha": "d" * 40,
                "base_commit_sha": "0" * 40,
                "start_commit_sha": "0" * 40,
            },
            {
                "head_commit_sha": HEAD,
                "base_commit_sha": "b" * 40,
                "start_commit_sha": "s" * 40,
            },
        ],
    )
    placed: list[Any] = []

    def discussion(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["position"]["new_path"] == "gone.py":
            return httpx.Response(400, json={"message": "line_code can't be blank"})
        placed.append(body)
        return httpx.Response(201, json={"id": "d9"})

    fake.on("POST", f"{mr}/discussions", None)
    fake.handle_orig = fake.handle  # type: ignore[attr-defined]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/discussions"):
            return discussion(request)
        return fake.handle_orig(request)  # type: ignore[attr-defined]

    fake.on("POST", f"{mr}/notes", {"id": 55})

    async def token() -> str:
        return "t"

    client = GitLabClient(API, token, transport=httpx.MockTransport(handle))
    writer = GitLabWriter(client, "https://gitlab.corp.com")
    ref = await writer.create_review(
        REPO,
        7,
        HEAD,
        public("Summary"),
        "REQUEST_CHANGES",
        [
            ReviewCommentSpec(
                path="a.py",
                line=4,
                start_line=2,
                body=public("Try this:\n```suggestion\nfaster()\n```"),
            ),
            ReviewCommentSpec(path="gone.py", line=1, body=public("Elsewhere")),
        ],
    )
    [put] = placed
    assert (
        put["position"]["head_sha"] == HEAD and put["position"]["base_sha"] == "b" * 40
    )
    assert put["position"]["new_line"] == 4
    assert "```suggestion:-2+0" in put["body"]
    [note] = [b for m, p, b in fake.sent if m == "POST" and p.endswith("/notes")]
    assert note["body"].startswith("Summary") and "**gone.py:1**" in note["body"]
    assert ref.id == 55 and ref.url.endswith("/grp/sub/proj/-/merge_requests/7#note_55")
    with pytest.raises(ValueError, match="checked for"):
        await writer.create_comment(REPO, 7, public("x", repo="acme/widget"))


async def test_approving_rebasing_and_merging() -> None:
    fake = FakeGitLab()
    mr = f"/projects/{PID}/merge_requests/7"
    fake.on("GET", mr, {"sha": HEAD, "title": "T", "draft": False})
    fake.on("PUT", mr, {"iid": 7})
    fake.on("PUT", f"{mr}/rebase", {"rebase_in_progress": True})
    fake.on("POST", f"{mr}/approve", {"approved": True})
    fake.on(
        "PUT",
        f"{mr}/merge",
        {"state": "merged", "merge_commit_sha": "m" * 40},
    )
    service = GitLabWriter(fake.client(), "https://gitlab.corp.com")
    with pytest.raises(PermissionError):
        await service.approve_pr(REPO, 7, HEAD, public(""))
    maria = GitLabWriter(fake.client(), "https://gitlab.corp.com", person="corp:maria")
    await maria.approve_pr(REPO, 7, HEAD, public(""))
    assert ("POST", f"{mr}/approve", {"sha": HEAD}) in fake.sent
    with pytest.raises(GitLabError, match="head moved"):
        await service.update_branch(REPO, 7, "e" * 40)
    await service.update_branch(REPO, 7, HEAD)
    sha = await service.merge(REPO, 7, HEAD, "squash", public("Speed"), public("Body"))
    assert sha == "m" * 40
    [merge] = [b for m, p, b in fake.sent if p.endswith("/merge")]
    assert merge == {
        "sha": HEAD,
        "squash": True,
        "squash_commit_message": "Speed\n\nBody",
    }
    await service.convert_to_draft(REPO, 7)
    assert ("PUT", mr, {"title": "Draft: T"}) in fake.sent

    fake.on(
        "POST",
        f"/projects/{PID}/merge_requests",
        {
            "iid": 8,
            "web_url": "https://gitlab.corp.com/grp/sub/proj/-/merge_requests/8",
        },
    )
    opened = await maria.create_pr(
        REPO, "am/pr-1", "main", public("docs: add skill"), public("Body"), draft=True
    )
    assert opened.id == 8
    [mr_post] = [
        b for m, p, b in fake.sent if m == "POST" and p.endswith("/merge_requests")
    ]
    assert mr_post == {
        "source_branch": "am/pr-1",
        "target_branch": "main",
        "title": "Draft: docs: add skill",
        "description": "Body",
    }


async def test_ci_retries_a_pipeline_and_reads_a_job_log() -> None:
    fake = FakeGitLab()
    fake.on("POST", f"/projects/{PID}/pipelines/900/retry", {"id": 900})
    fake.on(
        "GET", f"/projects/{PID}/jobs/31/trace", httpx.Response(200, text="log line")
    )
    ci = GitLabCi(fake.client())
    await ci.rerun_failed_jobs(REPO, 900)
    assert await ci.get_job_log(REPO, 31) == "log line"


def test_webhooks_carry_the_shared_token_and_name_the_merge_request() -> None:
    assert verify_token(b"s3cret", b"{}", "s3cret")
    assert not verify_token(b"s3cret", b"{}", "nope") and not verify_token(
        b"s3cret", b"{}", None
    )
    payload = {
        "object_kind": "note",
        "project": {"path_with_namespace": "grp/sub/proj"},
        "merge_request": {"iid": 7},
    }
    assert hint("Note Hook", payload) == ("grp/sub/proj", 7)
    assert hint("Push Hook", {"object_kind": "push"}) is None
