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

"""GitHubReader: snapshot mapping, two-pass listing, repo facts, people."""

from __future__ import annotations

import json

import httpx
from gh_testutil import load_fixture

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckStatus,
    Mergeable,
    PrState,
    ReviewEventState,
)
from auto_maintainer.forges.github import GitHubReader, snapshot_from_graphql
from auto_maintainer.forges.github.reader import map_check_conclusion, map_check_status


def test_snapshot_mapping():
    pr = snapshot_from_graphql("acme/widget", load_fixture("pr_node.json"))
    assert (pr.number, pr.author, pr.state, pr.is_draft) == (
        42,
        "alice",
        PrState.OPEN,
        False,
    )
    assert pr.head_repo == "alice/widget" and pr.base_ref == "main"
    assert pr.mergeable == Mergeable.MERGEABLE and pr.behind_base is True
    assert pr.labels == ["bug", "needs review"]
    assert pr.paths == ["widget/scheduler/ports.py", "tests/test_ports.py"]
    assert pr.commits[0].trailers == [("Signed-off-by", "Alice <alice@example.com>")]
    assert (
        pr.commits[1].author_login is None
        and pr.commits[1].author_email == "bob@example.com"
    )
    assert pr.reviews[0].state == ReviewEventState.CHANGES_REQUESTED
    assert pr.reviews[0].commit_sha.startswith("aaaa1111")
    assert pr.reviews[1].author == "ghost" and pr.reviews[1].commit_sha is None
    assert pr.comments[0].id == 7001 and pr.comments[0].body == "/am review"
    thread = pr.threads[0]
    assert (
        thread.is_outdated and not thread.is_resolved and thread.comments[0].id == 8001
    )
    # People and teams apart: only people own a PR.
    assert pr.requested_reviewers == ["carol"] and pr.requested_teams == ["core-team"]
    assert pr.assignees == ["dave"]
    checks = {c.name: c for c in pr.checks}
    assert set(checks) == {"unit", "lint", "e2e", "codecov/patch"}
    unit = checks["unit"]
    assert (unit.id, unit.workflow, unit.run_id, unit.run_attempt) == (1, "CI", 555, 2)
    assert (
        unit.status == CheckStatus.COMPLETED
        and unit.conclusion == CheckConclusion.FAILURE
        and unit.failed
    )
    assert (
        checks["e2e"].status == CheckStatus.IN_PROGRESS
        and checks["e2e"].conclusion is None
    )
    assert (
        checks["codecov/patch"].workflow is None
        and checks["codecov/patch"].status == CheckStatus.QUEUED
    )
    assert pr.created_at.tzinfo is not None


def test_status_and_conclusion_mapping():
    assert map_check_status("COMPLETED") == CheckStatus.COMPLETED
    assert map_check_status("pending") == CheckStatus.QUEUED
    assert map_check_status(None) == CheckStatus.QUEUED
    assert map_check_conclusion("TIMED_OUT") == CheckConclusion.TIMED_OUT
    assert map_check_conclusion("STARTUP_FAILURE") == CheckConclusion.STARTUP_FAILURE
    assert map_check_conclusion(None) is None
    assert map_check_conclusion("SOMETHING_NEW") is None


def _light(number: int, updated: str, suites: list[dict]) -> dict:
    return {
        "number": number,
        "updatedAt": updated,
        "headRefOid": "0ebdf9c3a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "checkSuites": {"totalCount": len(suites), "nodes": suites}
                    }
                }
            ]
        },
    }


async def test_list_open_prs_fetches_only_changed(client, router):
    state = {"suites": [{"updatedAt": "2026-10-02T09:10:00Z", "status": "IN_PROGRESS"}]}
    full_queries: list[str] = []

    def graphql(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "pageInfo" in query:
            nodes = [_light(42, "2026-10-02T09:30:00Z", state["suites"])]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": nodes,
                            }
                        }
                    }
                },
            )
        full_queries.append(query)
        return httpx.Response(
            200, json={"data": {"repository": {"p42": load_fixture("pr_node.json")}}}
        )

    router.post("/graphql").mock(side_effect=graphql)
    reader = GitHubReader(client)
    first = await reader.list_open_prs("acme/widget")
    second = await reader.list_open_prs("acme/widget")
    assert [p.number for p in first] == [42] and second == first
    assert len(full_queries) == 1, "an unchanged PR is served from the cache"
    state["suites"] = [{"updatedAt": "2026-10-02T09:20:00Z", "status": "COMPLETED"}]
    await reader.list_open_prs("acme/widget")
    assert len(full_queries) == 2, "a check suite update refetches the PR"
    assert "p42: pullRequest(number: 42)" in full_queries[0]


async def test_list_open_prs_refetches_after_cache_age(client, router):
    now = [1000.0]

    def graphql(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "pageInfo" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [_light(42, "2026-10-02T09:30:00Z", [])],
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            200, json={"data": {"repository": {"p42": load_fixture("pr_node.json")}}}
        )

    route = router.post("/graphql").mock(side_effect=graphql)
    reader = GitHubReader(client, snapshot_cache_seconds=60, clock=lambda: now[0])
    await reader.list_open_prs("acme/widget")
    now[0] += 61
    await reader.list_open_prs("acme/widget")
    assert route.call_count == 4


async def test_get_pr_missing_returns_none(client, router):
    router.post("/graphql").respond(
        200,
        json={
            "data": {"repository": {"p7": None}},
            "errors": [{"type": "NOT_FOUND", "message": "no PR"}],
        },
    )
    assert await GitHubReader(client).get_pr("acme/widget", 7) is None


async def test_get_repo_with_unreadable_protection(client, router):
    router.get("/repos/acme/widget").respond(
        200, json={"default_branch": "main", "private": True}
    )
    router.get("/repos/acme/widget/branches/main").respond(
        200,
        json={
            "commit": {"sha": "abc123"},
            "protected": True,
            "protection": {
                "required_status_checks": {
                    "contexts": ["lint"],
                    "checks": [{"context": "unit"}],
                }
            },
        },
    )
    router.get("/repos/acme/widget/branches/main/protection").respond(
        403, json={"message": "admin only"}
    )
    router.get("/repos/acme/widget/rules/branches/main").respond(
        200,
        json=[
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 2,
                    "require_code_owner_review": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "e2e"}],
                },
            },
        ],
    )
    router.get("/repos/acme/widget/labels").respond(200, json=[{"name": "bug"}])
    info = await GitHubReader(client).get_repo("acme/widget")
    assert info.default_branch_sha == "abc123" and info.private
    assert info.required_checks == ["e2e", "lint", "unit"]
    assert info.requires_up_to_date is True and info.required_approvals == 2
    assert info.requires_code_owner_reviews is True, "from the branch's ruleset"
    assert info.labels == ["bug"]


async def test_get_repo_unprotected_branch(client, router):
    router.get("/repos/acme/widget").respond(200, json={"default_branch": "dev"})
    router.get("/repos/acme/widget/branches/dev").respond(
        200, json={"commit": {"sha": "s"}, "protected": False}
    )
    router.get("/repos/acme/widget/rules/branches/dev").respond(200, json=[])
    router.get("/repos/acme/widget/labels").respond(200, json=[])
    info = await GitHubReader(client).get_repo("acme/widget")
    assert (
        info.required_checks,
        info.requires_up_to_date,
        info.required_approvals,
        info.requires_code_owner_reviews,
    ) == ([], False, 0, False)


async def test_permission_mapping(client, router):
    router.get("/repos/acme/widget/collaborators/maint/permission").respond(
        200, json={"permission": "write", "role_name": "maintain"}
    )
    router.get("/repos/acme/widget/collaborators/old/permission").respond(
        200, json={"permission": "write"}
    )
    router.get("/repos/acme/widget/collaborators/nobody/permission").respond(
        404, json={"message": "Not Found"}
    )
    reader = GitHubReader(client)
    assert await reader.get_permission("acme/widget", "maint") == Permission.MAINTAIN
    assert await reader.get_permission("acme/widget", "old") == Permission.WRITE
    assert await reader.get_permission("acme/widget", "nobody") == Permission.NONE


async def test_team_membership(client, router):
    router.get("/orgs/acme/teams/core/memberships/alice").respond(
        200, json={"state": "active"}
    )
    router.get("/orgs/acme/teams/core/memberships/bob").respond(
        200, json={"state": "pending"}
    )
    router.get("/orgs/acme/teams/core/memberships/eve").respond(404)
    reader = GitHubReader(client)
    assert await reader.is_team_member("acme/widget", "acme/core", "alice")
    assert not await reader.is_team_member("acme/widget", "acme/core", "bob")
    assert not await reader.is_team_member("acme/widget", "acme/core", "eve")


async def test_get_file_and_list_dir(client, router):
    router.get(
        "/repos/acme/widget/contents/.github/auto-maintainer.yml",
        params={"ref": "main"},
    ).respond(
        200, json={"type": "file", "encoding": "base64", "content": "c2NoZW1hOiAx\n"}
    )
    router.get("/repos/acme/widget/contents/missing.txt").respond(404)
    router.get("/repos/acme/widget/contents/.github").respond(
        200,
        json=[
            {"name": "workflows", "type": "dir"},
            {"name": "CODEOWNERS", "type": "file"},
        ],
    )
    reader = GitHubReader(client)
    assert (
        await reader.get_file("acme/widget", ".github/auto-maintainer.yml", ref="main")
        == "schema: 1"
    )
    assert await reader.get_file("acme/widget", "missing.txt") is None
    assert await reader.get_file("acme/widget", ".github") is None
    assert await reader.list_dir("acme/widget", ".github") == [
        "workflows",
        "CODEOWNERS",
    ]
    assert await reader.list_dir("acme/widget", "missing.txt") == []


async def test_path_changed_by_merged_pr(client, router):
    router.get(
        "/repos/acme/widget/commits", params={"path": "CONTRIBUTING.md"}
    ).respond(
        200,
        json=[
            {
                "sha": "c1",
                "committer": {"login": "web-flow"},
                "author": {"login": "alice"},
            }
        ],
    )
    router.get("/repos/acme/widget/commits/c1/pulls").respond(
        200, json=[{"number": 5, "merged_at": "2026-10-01T00:00:00Z"}]
    )
    router.get("/repos/acme/widget/pulls/5").respond(
        200, json={"merged_by": {"login": "maint"}}
    )
    assert (
        await GitHubReader(client).path_changed_by("acme/widget", "CONTRIBUTING.md")
        == "maint"
    )


async def test_path_changed_by_direct_push_and_missing(client, router):
    route = router.get("/repos/acme/widget/commits").respond(
        200,
        json=[
            {
                "sha": "c2",
                "committer": {"login": "web-flow"},
                "author": {"login": "alice"},
            }
        ],
    )
    router.get("/repos/acme/widget/commits/c2/pulls").respond(200, json=[])
    reader = GitHubReader(client)
    assert (
        await reader.path_changed_by("acme/widget", "README.md", ref="dev") == "alice"
    )
    assert route.calls[0].request.url.params["sha"] == "dev"
    route.respond(200, json=[])
    assert await reader.path_changed_by("acme/widget", "nothing.md") is None


async def test_default_branch_sha_and_service_login(client, router):
    router.get("/repos/acme/widget").respond(200, json={"default_branch": "main"})
    router.get("/repos/acme/widget/git/ref/heads/main").respond(
        200, json={"object": {"sha": "deadbeef"}}
    )
    router.get("/user").respond(200, json={"login": "svc-bot"})
    reader = GitHubReader(client)
    assert await reader.default_branch_sha("acme/widget") == "deadbeef"
    assert await reader.service_login() == "svc-bot"


async def test_open_issues_exclude_prs(client, router):
    router.get("/repos/acme/widget/issues").respond(
        200,
        json=[
            {
                "number": 1,
                "title": "bug",
                "user": {"login": "u"},
                "labels": [{"name": "bug"}],
            },
            {"number": 2, "title": "a PR", "user": {"login": "u"}, "pull_request": {}},
        ],
    )
    issues = await GitHubReader(client).list_open_issues("acme/widget")
    assert [(i.number, i.labels) for i in issues] == [(1, ["bug"])]


async def test_merged_prs_newest_first(client, router):
    router.post("/graphql").respond(
        200,
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [
                            {
                                "number": 1,
                                "title": "old",
                                "mergedAt": "2026-01-01T00:00:00Z",
                                "author": {"login": "a"},
                                "labels": {"nodes": []},
                                "reviews": {"totalCount": 1},
                                "commits": {"nodes": [{"commit": {"message": "m1"}}]},
                            },
                            {
                                "number": 2,
                                "title": "new",
                                "mergedAt": "2026-02-01T00:00:00Z",
                                "author": None,
                                "labels": {"nodes": [{"name": "bug"}]},
                                "reviews": {"totalCount": 0},
                                "commits": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        },
    )
    merged = await GitHubReader(client).list_merged_prs("acme/widget", limit=2)
    assert [m.number for m in merged] == [2, 1]
    assert (
        merged[0].author == "ghost"
        and merged[1].commit_messages == ["m1"]
        and merged[1].review_count == 1
    )


def test_a_workflow_github_holds_for_approval_is_a_check_that_has_not_run():
    node = load_fixture("pr_node.json")
    suites = node["headCommit"]["nodes"][0]["commit"]["checkSuites"]["nodes"]
    suites.append(
        {
            "status": "COMPLETED",
            "conclusion": "ACTION_REQUIRED",
            "workflowRun": {
                "databaseId": 991,
                "runAttempt": 1,
                "url": "https://github.com/acme/widget/actions/runs/991",
                "workflow": {"name": "CI"},
            },
            "checkRuns": {"nodes": []},
        }
    )
    pr = snapshot_from_graphql("acme/widget", node)
    [held] = [c for c in pr.checks if c.awaiting_approval]
    assert (held.name, held.workflow, held.run_id) == ("CI", "CI", 991)
    assert held.status != "completed" and not held.failed
    assert held.details_url == "https://github.com/acme/widget/actions/runs/991"
