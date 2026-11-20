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

import asyncio
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.access import (
    ABOUT,
    ACCESS_PATH,
    ADMIN_ONLY,
    DEFAULTS,
    GATED,
    GROUPS,
    ROLE_NAMES,
    Grant,
    PeopleRole,
    Role,
    default_role,
    dump_access,
    parse_access,
)
from .api import access_of, permissions_of, service_of, shows_owner
from .auth import User, current_user


class AccessBody(BaseModel):
    """The new ``config/access.yaml``: its ``text``, or its ``config`` as the page
    edits it (written as YAML by ``dump_access``)."""

    text: str | None = Field(default=None, max_length=200_000)
    config: dict[str, Any] | None = None
    title: str = Field(default="access: update", max_length=200)


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _effective(effective: Any, *, arming_who: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "repo": effective.repo,
        "role": effective.role,
        "role_from": effective.role_from,
        "github_permission": str(effective.github_permission),
        "maintains": effective.maintains,
        "grants": {str(g): why for g, why in effective.grants.items()},
        "withheld": {str(g): why for g, why in effective.withheld.items()},
        "limits": effective.limits.model_dump(),
    }
    if arming_who is not None:
        out["arming_who"] = arming_who
    return out


def _catalog() -> dict[str, Any]:
    """Every permission, grouped, and each role's defaults: what the file changes."""
    return {
        "groups": [
            {
                "name": name,
                "grants": [
                    {
                        "id": str(g),
                        "about": ABOUT[g],
                        "gated": g in GATED,
                        "admin_only": g in ADMIN_ONLY,
                    }
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
                profile = await service.profiles.active(record.repo)
                arming_who = (
                    profile.merge.arming.who
                    if profile is not None and profile.merge.enabled
                    else None
                )
                repos.append(_effective(effective, arming_who=arming_who))
        owner = (await shows_owner(service, user.login)).allowed
        return {
            "login": user.login,
            "owner": owner,
            "admin": service.access.is_admin(user.login),
            "manages": owner or service.access.is_admin(user.login),
            "repos": repos,
            **_catalog(),
        }

    @r.get("")
    async def read(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """``config/access.yaml``, whether it is in force, and what it changes."""
        service = await _manager(request, user)
        repos = []
        for record in await service.store.list_repos():
            loaded = await service.governance.load(record.repo)
            mapping = service.access.mapping(record.repo)
            roles = []
            if loaded.stored and not loaded.error:
                gov = loaded.governance
                for role in sorted(gov.roles, key=lambda r: -r.level):
                    held = PeopleRole(
                        id=role.id,
                        name=role.name,
                        maintains=role.maintains,
                        github_permission=role.github_permission,
                    )
                    roles.append(
                        {
                            "id": role.id,
                            "name": role.name,
                            "members": sum(1 for m in gov.members if m.role == role.id),
                            "maintains": role.maintains,
                            "default": default_role(held),
                            "mapped": mapping.get(role.id),
                        }
                    )
            profile = await service.profiles.active(record.repo)
            repos.append(
                {
                    "repo": record.repo,
                    "people_roles": roles,
                    "arming_who": profile.merge.arming.who if profile else "write",
                    "merge_enabled": bool(profile and profile.merge.enabled),
                }
            )
        return {
            "path": ACCESS_PATH,
            "text": await service.a.memory.read_text(ACCESS_PATH) or "",
            "problem": service.access_problem,
            "config": service.access.model_dump(mode="json", by_alias=True),
            "admins": service.access.admins,
            "owner": (await shows_owner(service, user.login)).allowed,
            "repos": repos,
            "agents": sorted(getattr(service.a.agents, "runtimes", {}) or {}),
            "machines": sorted(n for n, m in service.machines.items() if not m.owner),
            **_catalog(),
        }

    @r.get("/people")
    async def people(
        request: Request, user: User = Depends(current_user)
    ) -> list[dict[str, Any]]:
        """Everyone the service knows (signed in, in a People record, or in the
        file), with their role in each repository it serves and where it came
        from."""
        service = await _manager(request, user)
        served = [r.repo for r in await service.store.list_repos()]
        signed = {lg.lower(): lg for lg in await service.a.user_tokens.logins()}
        known: dict[str, str] = dict(signed)
        for repo in served:
            for login in await service.governance.members_of(repo):
                known.setdefault(login.lower(), login)
        for login in [*service.access.people, *service.access.admins]:
            known.setdefault(login.lower(), login)
        logins = sorted(known.values(), key=str.lower)
        perms = {repo: await permissions_of(service, repo, logins) for repo in served}
        gate = asyncio.Semaphore(8)

        async def one(login: str, repo: str) -> Any:
            async with gate:
                return await service.access_for(
                    login, repo, perms[repo].get(login.lower())
                )

        found = await asyncio.gather(
            *(one(login, repo) for login in logins for repo in served)
        )
        rows = []
        for i, login in enumerate(logins):
            here = found[i * len(served) : (i + 1) * len(served)]
            person = service.access.person(login)
            rows.append(
                {
                    "login": login,
                    "signed_in": login.lower() in signed,
                    "admin": service.access.is_admin(login),
                    "set": person.model_dump(mode="json") if person else None,
                    "repos": {
                        e.repo: {
                            "role": e.role,
                            "role_from": e.role_from,
                            "github_permission": str(e.github_permission),
                            "maintains": e.maintains,
                            "can_schedule": e.can(Grant.MERGE_SCHEDULE),
                            "can_configure": e.can(Grant.REPO_CONFIGURE),
                        }
                        for e in here
                    },
                }
            )
        return rows

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
            if body.config is not None:
                text = dump_access(parse_access(dump_yaml(body.config)))
            elif body.text is not None:
                text = body.text
            else:
                raise ValueError("send the file's text or its config")
            change, decision = await service.change_access(text, user.login, body.title)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {
            "change": change.id,
            "state": change.state,
            "applied": service.access_problem is None and change.state == "merged",
            "reason": decision.reason,
        }

    return r
