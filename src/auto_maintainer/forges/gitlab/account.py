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

"""GitLab account operations for setting a host up (``ForgeAccount``): one account's
view of a project, its commits, and making private projects, forks, and imports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from ...domain.permissions import Permission
from ..base import AccountError, ForgeAccount, RepoAccess
from .client import GitLabClient, GitLabError
from .reader import ROLES

COPY_SECONDS = 180.0
"""How long a fork or an import may take to finish."""

_HIDDEN = (401, 403, 404)


@asynccontextmanager
async def _client(api_url: str, token: str) -> AsyncIterator[GitLabClient]:
    async def fixed() -> str:
        return token

    client = GitLabClient(api_url, fixed, timeout=30)
    try:
        yield client
    finally:
        await client.aclose()


def _id(path: str) -> str:
    return quote(path, safe="")


def _access(data: dict[str, Any]) -> RepoAccess:
    perms = data.get("permissions") or {}
    level = max(
        int((perms.get("project_access") or {}).get("access_level") or 0),
        int((perms.get("group_access") or {}).get("access_level") or 0),
    )
    return RepoAccess(
        path=str(data["path_with_namespace"]),
        private=data.get("visibility") == "private",
        permission=ROLES.get(level, Permission.NONE),
        default_branch=str(data.get("default_branch") or "main"),
        web_url=str(data.get("web_url") or ""),
    )


async def repo(api_url: str, token: str, path: str) -> RepoAccess | None:
    async with _client(api_url, token) as client:
        try:
            data = await client.get(f"/projects/{_id(path)}", allow=_HIDDEN)
        except GitLabError as e:
            raise AccountError(f"{e} ({path})") from e
    return _access(data) if data else None


async def has_commit(api_url: str, token: str, path: str, sha: str) -> bool:
    async with _client(api_url, token) as client:
        try:
            found = await client.get(
                f"/projects/{_id(path)}/repository/commits/{sha}", allow=(404,)
            )
        except GitLabError as e:
            raise AccountError(f"{e} ({path}@{sha[:12]})") from e
    return found is not None


async def _wait(client: GitLabClient, project: dict[str, Any]) -> None:
    """Until a fork or import has finished copying."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COPY_SECONDS
    while True:
        state = await client.get(f"/projects/{project['id']}/import", allow=_HIDDEN)
        status = (state or {}).get("import_status") or "finished"
        if status in ("finished", "none"):
            return
        if status == "failed":
            raise AccountError(
                f"GitLab could not copy into {project['path_with_namespace']}: "
                f"{(state or {}).get('import_error') or 'no reason given'}"
            )
        if loop.time() > deadline:
            raise AccountError(
                f"GitLab is still copying into {project['path_with_namespace']}; "
                "try again in a minute"
            )
        await asyncio.sleep(3)


async def create_private(
    api_url: str, token: str, path: str, description: str
) -> RepoAccess:
    namespace, _, name = path.rpartition("/")
    async with _client(api_url, token) as client:
        try:
            me = await client.get("/user")
            body: dict[str, Any] = {
                "name": name,
                "path": name,
                "visibility": "private",
                "initialize_with_readme": True,
                "description": description,
            }
            if namespace and namespace.lower() != str(me["username"]).lower():
                ns = await client.get(f"/namespaces/{_id(namespace)}", allow=_HIDDEN)
                if ns is None:
                    raise AccountError(
                        f"there is no group {namespace} this account can create in"
                    )
                body["namespace_id"] = ns["id"]
            made = await client.request("POST", "/projects", json=body)
        except GitLabError as e:
            raise AccountError(f"GitLab did not create {path}: {e}") from e
    found = await repo(api_url, token, str(made["path_with_namespace"]))
    if found is None:
        raise AccountError(f"GitLab created {path} but this account cannot see it")
    return found


async def copy(
    api_url: str, token: str, source_web_url: str, source_path: str | None
) -> RepoAccess:
    """A fork when the source is on this GitLab, else a private import of it."""
    async with _client(api_url, token) as client:
        try:
            if source_path is not None:
                made = await client.request(
                    "POST", f"/projects/{_id(source_path)}/fork", json={}
                )
            else:
                name = source_web_url.rstrip("/").rsplit("/", 1)[-1]
                made = await client.request(
                    "POST",
                    "/projects",
                    json={
                        "name": name,
                        "path": name,
                        "import_url": source_web_url.rstrip("/") + ".git",
                        "visibility": "private",
                    },
                )
            await _wait(client, made)
        except GitLabError as e:
            raise AccountError(f"GitLab did not copy {source_web_url}: {e}") from e
    found = await repo(api_url, token, str(made["path_with_namespace"]))
    if found is None:
        raise AccountError("GitLab made the copy but this account cannot see it")
    return found


ACCOUNT = ForgeAccount(
    repo=repo, has_commit=has_commit, create_private=create_private, copy=copy
)
