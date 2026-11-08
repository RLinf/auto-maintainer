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

"""The GitHub read proxy: agents read GitHub as the service and cannot write."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from auto_maintainer.agents.github_proxy import (
    GitHubReadProxy,
    allowed,
    graphql_reads,
)


@pytest.mark.parametrize(
    ("document", "reads"),
    [
        ("query { viewer { login } }", True),
        (
            '{ repository(owner: "a", name: "b") { pullRequest(number: 1) { title } } }',
            True,
        ),
        (
            'query Q($n: Int!) { repository(owner: "a", name: "b") { id } }\nfragment F on PullRequest { id }',
            True,
        ),
        (
            'query { search(query: "mutation subscription", type: ISSUE) { issueCount } }',
            True,
        ),
        ("# mutation in a comment\nquery { viewer { login } }", True),
        ('query { a(b: """mutation { x }""") { id } }', True),
        (
            'mutation { addComment(input: {subjectId: "x", body: "y"}) { clientMutationId } }',
            False,
        ),
        ("query A { viewer { login } } mutation B { x }", False),
        ("subscription { s }", False),
    ],
)
def test_graphql_queries_read_and_mutations_do_not(document: str, reads: bool) -> None:
    assert graphql_reads(document) is reads


def test_only_reads_go_through() -> None:
    api = "api.github.com"
    q = json.dumps({"query": "query { viewer { login } }"}).encode()
    m = json.dumps({"query": "mutation { x }"}).encode()
    assert allowed("GET", api, "/repos/a/b/pulls/1/reviews", b"", api)
    assert allowed("GET", "objects.githubusercontent.com", "/logs", b"", api)
    assert allowed("POST", api, "/graphql", q, api)
    assert allowed(
        "POST", api, "/graphql", json.dumps([{"query": "{ a }"}]).encode(), api
    )
    assert not allowed("POST", api, "/graphql", m, api)
    assert not allowed("POST", api, "/graphql", b"not json", api)
    assert not allowed("POST", api, "/repos/a/b/issues/1/comments", b"{}", api)
    assert not allowed("PATCH", api, "/repos/a/b/pulls/1", b"{}", api)
    assert not allowed("DELETE", api, "/repos/a/b/git/refs/heads/x", b"", api)
    assert not allowed("PUT", api, "/repos/a/b/pulls/1/merge", b"{}", api)


async def test_the_proxy_reads_with_the_services_token_and_refuses_writes() -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"full_name": "RLinf/RLinf"})

    async def token() -> str:
        return "gho_service"

    with tempfile.TemporaryDirectory(prefix="am-") as tmp:
        socket = Path(tmp) / "gh.sock"
        proxy = GitHubReadProxy(socket, token, transport=httpx.MockTransport(upstream))
        await proxy.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                base_url="http://api.github.com",
            ) as gh:
                ok = await gh.get(
                    "/repos/RLinf/RLinf",
                    headers={"Authorization": "token am-read-proxy"},
                )
                assert ok.status_code == 200 and ok.json()["full_name"] == "RLinf/RLinf"
                assert str(seen[0].url) == "https://api.github.com/repos/RLinf/RLinf"
                assert seen[0].headers["authorization"] == "token gho_service"
                refused = await gh.post(
                    "/repos/RLinf/RLinf/issues/1/comments", json={"body": "hi"}
                )
                assert refused.status_code == 403 and len(seen) == 1
                elsewhere = await gh.get(
                    "http://objects.githubusercontent.com/x",
                    headers={"Host": "objects.githubusercontent.com"},
                )
                assert elsewhere.status_code == 200
                assert "authorization" not in seen[-1].headers, "the token stays home"
        finally:
            await proxy.stop()
