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

"""Display names on the dashboard (design 5.13): the name each login goes by, such
as a person's real name, so people recognize each other behind their logins.

Everyone signed in reads them all. A person sets or clears their own; the service's
owner may name a login whose person has set none (people who never sign in, from a
team's list), and a person's own name always replaces it. Two people never share a
name, and the dashboard shows the login beside every name, so nobody passes for
someone else. Names are internal: nothing the service posts uses them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.ids import check_login
from ..domain.records import AuditEntry, PersonAlias, clean_alias
from ..service import Service
from .api import service_of
from .auth import User, current_user


class AliasBody(BaseModel):
    alias: str | None = Field(default=None, max_length=200)
    """The name; empty or ``null`` clears it."""


async def _view(service: Service, user: User) -> dict[str, Any]:
    return {
        "aliases": [
            {"login": a.login, "alias": a.alias, "own": a.own}
            for a in await service.store.list_aliases()
        ],
        "can_name_others": (await service.service_gate(user.login)).allowed,
    }


async def _set(service: Service, login: str, text: str | None, by: str) -> None:
    """Give ``login`` the name ``text`` (``None`` or blank: none), as ``by``."""
    if not (text or "").strip():
        await service.store.delete_alias(login)
        summary = f"cleared the display name of {login}"
    else:
        try:
            alias = clean_alias(text or "")
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        for other in await service.store.list_aliases():
            if other.login.lower() != login.lower() and (
                other.alias.lower() == alias.lower()
                or other.login.lower() == alias.lower()
            ):
                raise HTTPException(
                    409, f"{other.login} goes by {other.alias!r}; choose another name"
                )
        await service.store.set_alias(
            PersonAlias(
                login=login, alias=alias, set_by=by, updated_at=service.clock.now()
            )
        )
        summary = f"named {login} {alias!r}"
    await service.store.add_audit(AuditEntry(kind="alias", actor=by, summary=summary))


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/aliases")
    async def aliases(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Every login's display name, and whether the user may name others."""
        return await _view(service_of(request), user)

    @r.put("/me/alias")
    async def set_mine(
        request: Request, body: AliasBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Set or clear the name the user goes by; it replaces one the owner gave."""
        service = service_of(request)
        await _set(service, user.login, body.alias, user.login)
        return await _view(service, user)

    @r.put("/aliases/{login}")
    async def set_theirs(
        request: Request,
        login: str,
        body: AliasBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Name a login whose person has set no name of their own: the service's
        owner only. A name the person set is theirs to change."""
        service = service_of(request)
        try:
            login = check_login(login)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if login.lower() == user.login.lower():
            await _set(service, login, body.alias, user.login)
            return await _view(service, user)
        decision = await service.service_gate(user.login)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        current = next(
            (
                a
                for a in await service.store.list_aliases()
                if a.login.lower() == login.lower()
            ),
            None,
        )
        if current is not None and current.own:
            raise HTTPException(
                409, f"{current.login} chose their own name; only they change it"
            )
        await _set(service, login, body.alias, user.login)
        return await _view(service, user)

    return r
