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

"""The People page of a repository (design 2.6): its governance record, where the
record and GitHub disagree, the changes to it that wait for a maintainer, and asking
the agent to fill it in or to check who should change role.

Everyone who may see the repository (write access) may read the record and ask the
agent; what the agent drafts is a memory change that only a maintainer of the
repository approves, on the Memory page or here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.codeowners import CODEOWNERS_LOCATIONS, parse_codeowners
from ..domain.events import Event, EventKind
from ..domain.governance import Governance, role_changes
from ..domain.memory import MemoryChangeState
from ..domain.permissions import Permission
from ..domain.secrets import redact_secrets
from ..profiles.governance import check, governance_path
from ..service import Service
from .api import _repo, require_view, service_of
from .auth import User, current_user

MAX_CHECKED = 60
"""Members whose GitHub permission the page reads; a larger record is checked in part."""


class AskBody(BaseModel):
    instruction: str = Field(default="", max_length=8_000)
    mode: Literal["edit", "members"] = "edit"
    agent: str | None = None


async def _codeowners(service: Service, repo: str, path: str | None) -> set[str]:
    for location in [path] if path else list(CODEOWNERS_LOCATIONS):
        text = await service.a.forge.get_file(repo, location)
        if text:
            return {
                owner.lower()
                for rule in parse_codeowners(text)
                for owner in rule.owners
                if "/" not in owner and "@" not in owner
            }
    return set()


async def _permissions(
    service: Service, repo: str, logins: list[str]
) -> dict[str, Permission]:
    async def one(login: str) -> tuple[str, Permission | None]:
        try:
            return login.lower(), await service.a.forge.get_permission(repo, login)
        except Exception:  # an unknown login or a forge hiccup leaves it unchecked
            return login.lower(), None

    found = await asyncio.gather(*(one(login) for login in logins[:MAX_CHECKED]))
    return {login: p for login, p in found if p is not None}


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/repos")

    @r.get("/{owner}/{name}/governance")
    async def governance(
        request: Request, owner: str, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The record (or the skeleton), where it disagrees with GitHub, and the
        changes to it that wait for a maintainer."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        loaded = await service.governance.load(repo)
        gov = loaded.governance
        logins = sorted({m.login for m in gov.members}, key=str.lower)
        permissions, codeowners = await asyncio.gather(
            _permissions(service, repo, logins),
            _codeowners(service, repo, profile.people.codeowners if profile else None),
        )
        path = governance_path(repo)
        pending = []
        for c in await service.store.list_memory_changes(MemoryChangeState.OPEN):
            if path not in c.files:
                continue
            try:
                drafted = Governance.model_validate(
                    {**(yaml.safe_load(c.files[path] or "") or {}), "repo": repo}
                )
            except (yaml.YAMLError, ValueError, TypeError):
                drafted = None
            pending.append(
                {
                    "id": c.id,
                    "role_changes": [
                        {"login": login, "from": old, "to": new}
                        for login, old, new in role_changes(gov, drafted)
                    ]
                    if drafted
                    else [],
                }
            )
        gate = (
            await service.gate.check(repo, user.login, profile, person=True)
            if profile
            else None
        )
        return {
            "repo": repo,
            "governance": gov.dump(),
            "stored": loaded.stored,
            "error": loaded.error,
            "checks": [
                {"kind": f.kind, "login": f.login, "text": f.text}
                for f in check(gov, permissions, codeowners)
            ],
            "permissions": {login: str(p) for login, p in permissions.items()},
            "pending": pending,
            "can_configure": bool(gate and gate.allowed),
            "has_profile": profile is not None,
        }

    @r.post("/{owner}/{name}/governance/agent")
    async def ask(
        request: Request,
        owner: str,
        name: str,
        body: AskBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have the agent fill in or change the record (``edit``), or check who should
        change role (``members``). It drafts a memory change; nothing applies until a
        maintainer approves it."""
        service = service_of(request)
        repo = _repo(owner, name)
        await require_view(service, user, repo)
        if await service.profiles.active(repo) is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        instruction, _ = redact_secrets(body.instruction.strip())
        if body.mode == "edit" and not instruction:
            loaded = await service.governance.load(repo)
            if loaded.governance.established:
                raise HTTPException(
                    422, "say what to change, or where the process is written"
                )
        event = Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=repo,
            payload={
                "command": "governance",
                "by": user.login,
                "mode": body.mode,
                "instruction": instruction,
                "agent": body.agent,
            },
            source="dashboard",
        )
        await service.bus.publish(event)
        return {"event": event.id}

    return r
