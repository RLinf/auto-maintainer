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

"""The service's repository and its owner, on the Settings page (design 2.3).

One person owns a deployment: an admin of the service's repository. The owner hands
ownership to another person signed in to this dashboard who is an admin there, or
moves the service to another repository (a fork on any forge this host serves),
which must hold the code this host runs and take the service's pushes. On a host set
up before owners, any admin of the repository takes ownership. ``steps``
(``hostsetup.ForgeSteps``, from the composition root) reads and writes ``local.yaml``
and checks a repository with the service's own account.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.forges import forge_of, info_of, parse_person, parse_repo
from ..domain.permissions import Permission
from .api import service_of
from .auth import User, current_user


class TransferBody(BaseModel):
    login: str = Field(min_length=1, max_length=120)


class MoveBody(BaseModel):
    link: str = Field(min_length=1, max_length=500)
    owner: str | None = Field(default=None, max_length=120)
    """The owner on the new repository's forge, when that is another forge."""


def build_router(
    steps: Any,
    on_host_change: Callable[[Any], Awaitable[None]],
) -> APIRouter:
    r = APIRouter(prefix="/api/settings/owner")

    async def gate(request: Request, user: User) -> Any:
        decision = await service_of(request).service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        return decision

    async def signed_in_admin(service: Any, repo: str, login: str) -> str:
        """``login`` as a person on ``repo``'s forge who signed in to this dashboard
        and is an admin of ``repo``; else ``HTTPException``."""
        try:
            who = parse_person(login.strip())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        person = who.name
        if who.forge != forge_of(repo):
            raise HTTPException(
                422, f"{person} is not an account on the forge of {repo}"
            )
        tokens = service.a.user_tokens
        if tokens is None or await tokens.get(person) is None:
            raise HTTPException(
                422,
                f"{person} has not signed in to this dashboard; they sign in once, "
                "then you can hand ownership to them",
            )
        role = await service.a.forge.get_permission(repo, person)
        if role is not Permission.ADMIN:
            raise HTTPException(
                422, f"{person} has {role} on {repo}; the owner must be an admin of it"
            )
        return person

    @r.get("")
    async def view(request: Request, user: User = Depends(current_user)) -> Any:
        """The service's repository, its owner, and what the caller may do."""
        service = service_of(request)
        repo = service.settings.code_repo
        decision = await service.service_gate(user.login)
        link = None
        if repo:
            ref = parse_repo(repo)
            link = f"{info_of(repo).web_url.rstrip('/')}/{ref.path}"
        return {
            "repo": repo,
            "repo_link": link,
            "owner": service.settings.owner,
            "is_owner": decision.allowed and service.settings.owner is not None,
            "may_take": decision.allowed and service.settings.owner is None,
            "reason": "" if decision.allowed else decision.reason,
        }

    @r.post("/take")
    async def take(request: Request, user: User = Depends(current_user)) -> Any:
        """On a host set up before owners, an admin of the service's repository
        takes ownership."""
        await gate(request, user)
        service = service_of(request)
        if service.settings.owner is not None:
            raise HTTPException(409, f"{service.settings.owner} owns this service")
        steps.set_owner(user.login)
        service.settings.owner = user.login
        return {"owner": user.login}

    @r.post("/transfer")
    async def transfer(
        request: Request, body: TransferBody, user: User = Depends(current_user)
    ) -> Any:
        """Hand ownership to another person signed in here who is an admin of the
        service's repository."""
        await gate(request, user)
        service = service_of(request)
        repo = service.settings.code_repo
        assert repo  # the gate passed
        person = await signed_in_admin(service, repo, body.login)
        steps.set_owner(person)
        service.settings.owner = person
        return {"owner": person}

    @r.post("/repo")
    async def move(
        request: Request, body: MoveBody, user: User = Depends(current_user)
    ) -> Any:
        """Move the service to another repository: a fork that holds the code this
        host runs and takes the service's pushes, owned by an admin of it who signed
        in here. The service restarts with it."""
        await gate(request, user)
        service = service_of(request)
        try:
            forge, name = steps.locate(body.link)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        owner = await signed_in_admin(service, name, body.owner or user.login)
        checks, path, branch = await steps.check_repo(forge, parse_repo(name).path)
        failed = [c["text"] for c in checks if not c["ok"]]
        if failed:
            raise HTTPException(422, "; ".join(failed))
        # The forge's own spelling of the path, under the same forge prefix.
        name = f"{forge.id}:{path}" if ":" in name else path
        cfg = steps.with_repo(name, owner, branch)
        try:
            await on_host_change(cfg)
        except Exception as e:
            raise HTTPException(
                502, f"the service could not start with {name}: {e}"
            ) from e
        steps.keep(cfg)
        return {"repo": name, "owner": owner, "checks": checks}

    return r
