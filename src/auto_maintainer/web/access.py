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

"""Who may do what (design 2.7): each person reads their own role and permissions in
every repository the service serves; the service's owner and its service admins
read and change ``config/access.yaml``, which says it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.access import (
    ABOUT,
    ACCESS_PATH,
    DEFAULTS,
    GATED,
    GROUPS,
    ROLE_NAMES,
    Role,
)
from .api import access_of, service_of, shows_owner
from .auth import User, current_user


class AccessBody(BaseModel):
    text: str = Field(max_length=200_000)
    title: str = Field(default="access: update", max_length=200)


def _effective(effective: Any) -> dict[str, Any]:
    return {
        "repo": effective.repo,
        "role": effective.role,
        "role_from": effective.role_from,
        "grants": {str(g): why for g, why in effective.grants.items()},
        "withheld": {str(g): why for g, why in effective.withheld.items()},
        "limits": effective.limits.model_dump(),
    }


def _catalog() -> dict[str, Any]:
    """Every permission, grouped, and each role's defaults: what the file changes."""
    return {
        "groups": [
            {
                "name": name,
                "grants": [
                    {"id": str(g), "about": ABOUT[g], "gated": g in GATED}
                    for g in grants
                ],
            }
            for name, grants in GROUPS
        ],
        "roles": [
            {
                "id": str(role),
                "name": ROLE_NAMES[role],
                "grants": sorted(str(g) for g in DEFAULTS[role]),
            }
            for role in Role
        ],
    }


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/access")

    async def _manager(request: Request, user: User) -> Any:
        service = service_of(request)
        owner = (await shows_owner(service, user.login)).allowed
        if not (owner or service.access.is_admin(user.login)):
            raise HTTPException(
                403, "only the service's owner and its service admins manage access"
            )
        return service

    @r.get("/me")
    async def mine(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The person's role and permissions in each repository the service serves."""
        service = service_of(request)
        repos = []
        for record in await service.store.list_repos():
            effective = await access_of(service, user, record.repo)
            if effective.role is not None:
                repos.append(_effective(effective))
        return {
            "login": user.login,
            "owner": (await shows_owner(service, user.login)).allowed,
            "admin": service.access.is_admin(user.login),
            "repos": repos,
        }

    @r.get("")
    async def read(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """``config/access.yaml``, whether it is in force, and what it changes."""
        service = await _manager(request, user)
        return {
            "path": ACCESS_PATH,
            "text": await service.a.memory.read_text(ACCESS_PATH) or "",
            "problem": service.access_problem,
            "admins": service.access.admins,
            **_catalog(),
        }

    @r.get("/{repo:path}/people/{login}")
    async def of_person(
        request: Request, repo: str, login: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Someone's role and permissions in ``repo``, with where each came from."""
        service = await _manager(request, user)
        return _effective(await access_of(service, User(login=login), repo))

    @r.put("")
    async def change(
        request: Request, body: AccessBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Change ``config/access.yaml``: applied at once for the owner, and for a
        service admin unless it changes who the service admins are."""
        service = await _manager(request, user)
        try:
            change, decision = await service.change_access(
                body.text, user.login, body.title
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {
            "change": change.id,
            "state": change.state,
            "applied": service.access_problem is None and change.state == "merged",
            "reason": decision.reason,
        }

    return r
