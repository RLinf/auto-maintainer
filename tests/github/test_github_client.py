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

"""GitHubClient: pagination, ETag reuse, retries, rate limits, GraphQL errors."""

from __future__ import annotations

import time

import httpx
import pytest

from auto_maintainer.forges.github import GitHubError, GraphQLError, RateLimited


async def test_paginate_follows_link_headers(client, router):
    page2 = "https://api.github.com/repos/acme/widget/labels?per_page=100&page=2"
    router.get("/repos/acme/widget/labels", params={"page": "2"}).respond(
        200, json=[{"name": "c"}]
    )
    router.get("/repos/acme/widget/labels").respond(
        200,
        json=[{"name": "a"}, {"name": "b"}],
        headers={"Link": f'<{page2}>; rel="next"'},
    )
    items = await client.paginate("/repos/acme/widget/labels", repo="acme/widget")
    assert [i["name"] for i in items] == ["a", "b", "c"]


async def test_etag_304_returns_cached_body(client, router):
    route = router.get("/repos/acme/widget").mock(
        side_effect=[
            httpx.Response(
                200, json={"default_branch": "main"}, headers={"ETag": '"v1"'}
            ),
            httpx.Response(304),
        ]
    )
    first = await client.get("/repos/acme/widget")
    second = await client.get("/repos/acme/widget")
    assert first == second == {"default_branch": "main"}
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert route.calls[0].request.headers["User-Agent"] == "auto-maintainer"


async def test_retries_server_errors_then_succeeds(client, router):
    route = router.get("/repos/acme/widget").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.get("/repos/acme/widget") == {"ok": True}
    assert route.call_count == 3


async def test_gives_up_after_max_retries(client, router):
    router.get("/repos/acme/widget").respond(502)
    with pytest.raises(GitHubError) as err:
        await client.get("/repos/acme/widget")
    assert err.value.status == 502


async def test_secondary_rate_limit_retry_after(client, router):
    route = router.get("/repos/acme/widget").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"Retry-After": "3"},
                json={"message": "secondary rate limit"},
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.get("/repos/acme/widget") == {"ok": True}
    assert route.call_count == 2


async def test_primary_rate_limit_far_reset_raises(client, router):
    reset = str(int(time.time()) + 3600)
    router.get("/repos/acme/widget").respond(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
        json={"message": "API rate limit exceeded"},
    )
    with pytest.raises(RateLimited):
        await client.get("/repos/acme/widget")
    assert client.rate_limit_remaining == 0


async def test_plain_forbidden_is_not_retried(client, router):
    route = router.get("/repos/acme/widget").respond(
        403, json={"message": "Resource not accessible"}
    )
    with pytest.raises(GitHubError) as err:
        await client.get("/repos/acme/widget")
    assert err.value.status == 403 and route.call_count == 1


async def test_graphql_errors_raise_and_not_found_can_pass(client, router):
    router.post("/graphql").respond(
        200,
        json={
            "data": {"repository": {"p1": None}},
            "errors": [{"type": "NOT_FOUND", "message": "nope"}],
        },
    )
    with pytest.raises(GraphQLError):
        await client.graphql("query { x }")
    data = await client.graphql("query { x }", allow_not_found=True)
    assert data == {"repository": {"p1": None}}
