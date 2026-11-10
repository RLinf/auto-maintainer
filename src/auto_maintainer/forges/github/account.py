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

"""GitHub account operations for setting a host up (``ForgeAccount``): one account's
view of a repository, its commits, and making private repositories and forks."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...domain.permissions import Permission
from ..base import AccountError, ForgeAccount, RepoAccess

FORK_SECONDS = 60.0
"""How long a new fork may take to appear."""

_LEVELS = ("admin", "maintain", "push", "triage", "pull")


def _client(api_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=api_url.rstrip("/"),
        timeout=30,
        trust_env=True,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "auto-maintainer",
        },
    )


def _access(data: dict[str, Any]) -> RepoAccess:
    perms = data.get("permissions") or {}
    level = next(
        (Permission.parse(name) for name in _LEVELS if perms.get(name)),
        Permission.NONE,
    )
    return RepoAccess(
        path=str(data["full_name"]),
        private=bool(data.get("private")),
        permission=level,
        default_branch=str(data.get("default_branch") or "main"),
        web_url=str(data.get("html_url") or ""),
    )


def _message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message") or "")[:300]
    except ValueError:
        return r.text[:300]


async def repo(api_url: str, token: str, path: str) -> RepoAccess | None:
    async with _client(api_url, token) as client:
        r = await client.get(f"/repos/{path}")
    if r.status_code in (401, 403, 404):
        return None
    if r.status_code != 200:
        raise AccountError(f"GitHub answered {r.status_code} for {path}")
    return _access(r.json())


async def has_commit(api_url: str, token: str, path: str, sha: str) -> bool:
    async with _client(api_url, token) as client:
        r = await client.get(f"/repos/{path}/commits/{sha}")
    if r.status_code == 200:
        return True
    if r.status_code in (404, 409, 422):
        return False
    raise AccountError(f"GitHub answered {r.status_code} for {path}@{sha[:12]}")


async def create_private(
    api_url: str, token: str, path: str, description: str
) -> RepoAccess:
    owner, _, name = path.partition("/")
    async with _client(api_url, token) as client:
        me = await client.get("/user")
        if me.status_code != 200:
            raise AccountError("GitHub did not say who this account is")
        own = str(me.json().get("login", "")).lower() == owner.lower()
        r = await client.post(
            "/user/repos" if own else f"/orgs/{owner}/repos",
            json={
                "name": name,
                "private": True,
                "auto_init": True,
                "description": description,
            },
        )
    if r.status_code not in (200, 201):
        raise AccountError(
            f"GitHub did not create {path} ({r.status_code}): {_message(r)}"
        )
    found = await repo(api_url, token, str(r.json().get("full_name") or path))
    if found is None:
        raise AccountError(f"GitHub created {path} but this account cannot see it")
    return found


async def copy(
    api_url: str, token: str, source_web_url: str, source_path: str | None
) -> RepoAccess:
    """A fork in the account's own namespace; GitHub forks only repositories on the
    same GitHub."""
    if source_path is None:
        raise AccountError(
            f"GitHub forks only repositories on the same GitHub; create a repository "
            f"and push {source_web_url} to it, then name it here"
        )
    async with _client(api_url, token) as client:
        r = await client.post(
            f"/repos/{source_path}/forks", json={"default_branch_only": True}
        )
    if r.status_code not in (200, 202):
        raise AccountError(
            f"GitHub did not fork {source_path} ({r.status_code}): {_message(r)}"
        )
    path = str(r.json()["full_name"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FORK_SECONDS
    while True:
        found = await repo(api_url, token, path)
        if found is not None or loop.time() > deadline:
            break
        await asyncio.sleep(2)
    if found is None:
        raise AccountError(f"GitHub is still making {path}; try again in a minute")
    return found


ACCOUNT = ForgeAccount(
    repo=repo, has_commit=has_commit, create_private=create_private, copy=copy
)
