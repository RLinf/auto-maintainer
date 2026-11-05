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

"""A small async GitHub REST and GraphQL client.

GET responses are cached by URL with their ETag; a ``304 Not Modified`` returns the
cached body and does not count against the rate limit. Server errors and secondary
rate limits are retried with backoff. A primary rate limit that resets later than
``max_rate_wait`` raises ``RateLimited`` instead of sleeping, so callers can back off.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .auth import API_URL, API_VERSION, USER_AGENT, TokenProvider

_RETRY_STATUSES = {500, 502, 503, 504}


class GitHubError(RuntimeError):
    """A GitHub API call failed.

    Attributes:
        status: The HTTP status, or 0 for a transport failure.
        body: The decoded response body, when there is one.
    """

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.body = body


class RateLimited(GitHubError):
    """The rate limit is exhausted until ``reset_at`` (Unix time)."""

    def __init__(self, reset_at: float, body: Any = None) -> None:
        super().__init__(403, f"rate limited until {reset_at:.0f}", body)
        self.reset_at = reset_at


class GraphQLError(GitHubError):
    """A GraphQL response carried errors.

    Attributes:
        errors: The ``errors`` array.
        data: Any partial ``data``.
    """

    def __init__(self, errors: list[dict[str, Any]], data: Any = None) -> None:
        message = "; ".join(str(e.get("message", e)) for e in errors)
        super().__init__(200, message, {"errors": errors, "data": data})
        self.errors = errors
        self.data = data

    @property
    def not_found_only(self) -> bool:
        return all(e.get("type") == "NOT_FOUND" for e in self.errors)


class GitHubClient:
    """REST and GraphQL access with ETag caching, pagination, and retries.

    Args:
        tokens: Supplies the bearer token for each call.
        api_url: The REST API root; GraphQL lives at ``{api_url}/graphql``.
        timeout: Per-request timeout in seconds.
        max_retries: Retries after the first attempt for retryable failures.
        max_backoff: Upper bound in seconds for one backoff sleep.
        max_rate_wait: Longest wait for a rate-limit reset before raising ``RateLimited``.
        transport: An httpx transport, for tests.
        sleep: The sleep function, for tests.
        etag_cache_size: How many GET responses to keep for conditional requests.
    """

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        api_url: str = API_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
        max_backoff: float = 60.0,
        max_rate_wait: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        etag_cache_size: int = 2048,
    ) -> None:
        self.tokens = tokens
        self.api_url = api_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=timeout,
            trust_env=True,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        self._max_retries = max_retries
        self._max_backoff = max_backoff
        self._max_rate_wait = max_rate_wait
        self._sleep = sleep
        self._etags: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        self._etag_size = etag_cache_size
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def request(
        self,
        method: str,
        path: str,
        *,
        repo: str | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """Send one request with retries and return the final response, whatever its status.

        Args:
            method: The HTTP method.
            path: A path under ``api_url`` or an absolute URL.
            repo: The repository the call is about; selects the App installation.
            params: Query parameters.
            json: A JSON body.
            headers: Extra headers.
            token: A bearer token to use instead of asking the token provider.
            follow_redirects: Whether to follow redirects.

        Raises:
            GitHubError: The request could not be completed after retries.
            RateLimited: The primary rate limit resets later than ``max_rate_wait``.
        """
        attempt = 0
        while True:
            bearer = token or await self.tokens.token(repo)
            req_headers = {"Authorization": f"Bearer {bearer}", **(headers or {})}
            try:
                resp = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=req_headers,
                    follow_redirects=follow_redirects,
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise GitHubError(0, f"{method} {path}: {exc}") from exc
                await self._sleep(min(2.0**attempt, self._max_backoff))
                attempt += 1
                continue
            self._note_rate_limit(resp)
            wait = self._retry_wait(resp, attempt)
            if wait is None:
                return resp
            await self._sleep(wait)
            attempt += 1

    def _note_rate_limit(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None and remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        if reset is not None and reset.isdigit():
            self.rate_limit_reset = float(reset)

    def _retry_wait(self, resp: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying ``resp``, or ``None`` to return it."""
        status = resp.status_code
        if status in _RETRY_STATUSES:
            return (
                None
                if attempt >= self._max_retries
                else min(2.0**attempt, self._max_backoff)
            )
        if status not in (403, 429):
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            if attempt >= self._max_retries:
                return None
            return min(float(retry_after), self._max_backoff)
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = float(resp.headers.get("x-ratelimit-reset", "0") or 0)
            wait = max(reset - time.time(), 1.0)
            if wait > self._max_rate_wait or attempt >= self._max_retries:
                raise RateLimited(reset, _body(resp))
            return wait
        if status == 429 or "secondary rate limit" in resp.text.lower():
            return (
                None
                if attempt >= self._max_retries
                else min(2.0 ** (attempt + 2), self._max_backoff)
            )
        return None

    @staticmethod
    def _raise_for(resp: httpx.Response, method: str, path: str) -> None:
        if resp.status_code >= 400:
            body = _body(resp)
            message = (
                body.get("message", resp.reason_phrase)
                if isinstance(body, dict)
                else resp.reason_phrase
            )
            raise GitHubError(resp.status_code, f"{method} {path}: {message}", body)

    # ------------------------------------------------------------------ REST

    async def get(
        self,
        path: str,
        *,
        repo: str | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
        allow: tuple[int, ...] = (),
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET a JSON resource, using the ETag cache.

        Returns:
            The decoded body; ``None`` for a 404 when ``allow_404`` is set, or for any
            status in ``allow``.
        """
        body, _ = await self._get_with_links(
            path,
            repo=repo,
            params=params,
            allow_404=allow_404,
            allow=allow,
            token=token,
            headers=headers,
        )
        return body

    async def _get_with_links(
        self,
        path: str,
        *,
        repo: str | None,
        params: dict[str, Any] | None,
        allow_404: bool = False,
        allow: tuple[int, ...] = (),
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, str | None]:
        key = str(self._http.build_request("GET", path, params=params).url)
        if headers:
            key += "|" + "|".join(f"{k}={v}" for k, v in sorted(headers.items()))
        req_headers = dict(headers or {})
        cached = self._etags.get(key)
        if cached:
            req_headers["If-None-Match"] = cached[0]
        resp = await self.request(
            "GET", path, repo=repo, params=params, headers=req_headers, token=token
        )
        next_url = resp.links.get("next", {}).get("url")
        if resp.status_code == 304 and cached:
            self._etags.move_to_end(key)
            return cached[1], next_url
        if (resp.status_code == 404 and allow_404) or resp.status_code in allow:
            return None, None
        self._raise_for(resp, "GET", path)
        body = _body(resp)
        etag = resp.headers.get("etag")
        if etag:
            self._etags[key] = (etag, body)
            self._etags.move_to_end(key)
            while len(self._etags) > self._etag_size:
                self._etags.popitem(last=False)
        return body, next_url

    async def paginate(
        self,
        path: str,
        *,
        repo: str | None = None,
        params: dict[str, Any] | None = None,
        item_key: str | None = None,
        max_pages: int = 20,
        allow_404: bool = False,
    ) -> list[Any]:
        """GET every page of a list endpoint by following ``Link: rel="next"``.

        Args:
            item_key: For endpoints that wrap the list in an object, its key.
            max_pages: Stop after this many pages.
        """
        items: list[Any] = []
        url: str | None = path
        query = {"per_page": 100, **(params or {})}
        pages = 0
        while url and pages < max_pages:
            body, url = await self._get_with_links(
                url, repo=repo, params=query, allow_404=allow_404
            )
            query = None  # the next URL already carries the query
            pages += 1
            if body is None:
                break
            items.extend(body.get(item_key, []) if item_key else body)
        return items

    async def _send(
        self,
        method: str,
        path: str,
        *,
        repo: str | None,
        json: Any,
        allow: tuple[int, ...],
    ) -> Any:
        resp = await self.request(method, path, repo=repo, json=json)
        if resp.status_code in allow:
            return None
        self._raise_for(resp, method, path)
        return _body(resp)

    async def post(
        self,
        path: str,
        *,
        repo: str | None = None,
        json: Any = None,
        allow: tuple[int, ...] = (),
    ) -> Any:
        return await self._send("POST", path, repo=repo, json=json, allow=allow)

    async def patch(
        self,
        path: str,
        *,
        repo: str | None = None,
        json: Any = None,
        allow: tuple[int, ...] = (),
    ) -> Any:
        return await self._send("PATCH", path, repo=repo, json=json, allow=allow)

    async def put(
        self,
        path: str,
        *,
        repo: str | None = None,
        json: Any = None,
        allow: tuple[int, ...] = (),
    ) -> Any:
        return await self._send("PUT", path, repo=repo, json=json, allow=allow)

    async def delete(
        self, path: str, *, repo: str | None = None, allow: tuple[int, ...] = ()
    ) -> Any:
        return await self._send("DELETE", path, repo=repo, json=None, allow=allow)

    # ------------------------------------------------------------------ GraphQL

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        repo: str | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        """Run a GraphQL query and return its ``data``.

        Args:
            allow_not_found: Return partial data when every error is ``NOT_FOUND``.

        Raises:
            GraphQLError: The response carried other errors.
        """
        resp = await self.request(
            "POST",
            "/graphql",
            repo=repo,
            json={"query": query, "variables": variables or {}},
        )
        self._raise_for(resp, "POST", "/graphql")
        body = _body(resp)
        errors = body.get("errors") if isinstance(body, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        if errors:
            err = GraphQLError(errors, data)
            if not (allow_not_found and err.not_found_only):
                raise err
        return data or {}


def _body(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text
