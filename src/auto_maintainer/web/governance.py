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

"""The People page of a repository (design 2.6): its governance record, everyone
with access to the repository on GitHub, where the two disagree, the changes to the
record that wait for a maintainer, adding people to it, and asking the agent to fill
it in or to check who should change role.

Everyone who may see the repository (write access) may read the record, add people,
and ask the agent. What they add, and what the agent drafts, is a memory change that
only a maintainer of the repository approves, on the Memory page or here; a
maintainer's own addition to the applied record is approved by making it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.events import Event, EventKind
from ..domain.forges import parse_person
from ..domain.governance import Governance, Member, role_changes, suggested_role
from ..domain.memory import MemoryChange, MemoryChangeState
from ..domain.permissions import Permission
from ..domain.profile import RepoProfile
from ..domain.records import AuditEntry
from ..domain.secrets import redact_secrets
from ..playbooks.governance import base_record, revised
from ..profiles.governance import check, governance_path
from ..service import Service
from .api import (
    _repo,
    permission,
    read_codeowners,
    require_view,
    service_of,
    shows_gate,
)
from .auth import User, current_user

MAX_CHECKED = 60
"""Members whose GitHub permission the page reads; a larger record is checked in part."""


class NewMember(BaseModel):
    login: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=100)
    areas: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=500)


class AddBody(BaseModel):
    people: list[NewMember] = Field(min_length=1, max_length=200)


class UpdateMemberBody(BaseModel):
    role: str = Field(min_length=1, max_length=100)
    areas: list[str] | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    reason: str = Field(default="", max_length=500)


class AskBody(BaseModel):
    instruction: str = Field(default="", max_length=8_000)
    mode: Literal["edit", "members"] = "edit"
    agent: str | None = None


async def _codeowners(service: Service, repo: str, path: str | None) -> set[str]:
    _, rules = await read_codeowners(service, repo, path)
    return {
        owner.lower()
        for rule in rules
        for owner in rule.owners
        if "/" not in owner and "@" not in owner
    }


async def _collaborators(
    service: Service, repo: str
) -> dict[str, tuple[str, Permission]]:
    """Everyone with access to ``repo`` on its forge, by lowercase login: the login
    as the forge spells it and their permission, as the views keep it
    (``Service.view_cached``); empty when the service may not list them."""

    async def read() -> dict[str, tuple[str, Permission]]:
        found = await service.a.forge.list_collaborators(repo)
        return {login.lower(): (login, perm) for login, perm in found}

    try:
        return await service.view_cached(("collaborators", repo), read)
    except Exception:  # a forge hiccup: the tab shows the record alone
        return {}


async def _permissions(
    service: Service,
    repo: str,
    logins: list[str],
    collaborators: dict[str, Permission],
) -> dict[str, Permission]:
    """Members' permissions on ``repo``: from the forge's list of collaborators, and
    for a member it does not list (someone reading a public repository, or every
    member when the list is not open to the service) as the views keep them
    (``api.permission``), so the tab does not read the forge once per member on
    every load."""

    async def one(login: str) -> tuple[str, Permission | None]:
        if login.lower() in collaborators:
            return login.lower(), collaborators[login.lower()]
        try:
            return login.lower(), await permission(service, User(login=login), repo)
        except Exception:  # an unknown login or a forge hiccup leaves it unchecked
            return login.lower(), None

    found = await asyncio.gather(*(one(login) for login in logins[:MAX_CHECKED]))
    return {login: p for login, p in found if p is not None}


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/repos")

    @r.get("/{repo:path}/governance")
    async def governance(
        request: Request, repo: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The record (or the skeleton), where it disagrees with GitHub, and the
        changes to it that wait for a maintainer."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        loaded = await service.governance.load(repo)
        gov = loaded.governance
        logins = sorted({m.login for m in gov.members}, key=str.lower)
        on_forge = await _collaborators(service, repo)
        collaborators = {low: perm for low, (_, perm) in on_forge.items()}
        permissions, codeowners = await asyncio.gather(
            _permissions(service, repo, logins, collaborators),
            _codeowners(service, repo, profile.people.codeowners if profile else None),
        )
        path = governance_path(repo)
        pending = []
        for c in await service.store.list_memory_changes(MemoryChangeState.OPEN):
            if path not in c.files:
                continue
            drafted = service.governance.drafted(repo, c)
            pending.append(
                {
                    "id": c.id,
                    "governance": drafted.dump() if drafted else None,
                    "role_changes": [
                        {"login": login, "from": old, "to": new}
                        for login, old, new in role_changes(gov, drafted)
                    ]
                    if drafted
                    else [],
                }
            )
        gate = await shows_gate(service, user, repo, profile) if profile else None
        # Additions build on the draft that waits, else on the record: suggest roles
        # from the roles it has.
        _, base, _ = await base_record(
            service.governance,
            repo,
            await service.store.list_memory_changes(MemoryChangeState.OPEN),
        )
        on_github = [
            {
                "login": login,
                "permission": str(perm),
                "role": suggested_role(base, perm),
            }
            for _, (login, perm) in sorted(on_forge.items())
        ]
        configures = bool(gate and gate.allowed)
        signed_in = (
            sorted(await service.a.user_tokens.logins(), key=str.lower)
            if configures
            else []
        )
        return {
            "repo": repo,
            "governance": gov.dump(),
            "github": on_github,
            "signed_in": signed_in,
            "stored": loaded.stored,
            "error": loaded.error,
            "checks": [
                {"kind": f.kind, "login": f.login, "text": f.text}
                for f in check(gov, permissions, codeowners)
            ],
            "permissions": {login: str(p) for login, p in permissions.items()},
            "pending": pending,
            "can_configure": configures,
            "has_profile": profile is not None,
        }

    async def _submit_record(
        service: Service,
        user: User,
        repo: str,
        profile: RepoProfile,
        after: Governance,
        replaced: MemoryChange | None,
        *,
        title: str,
        description: str,
        audit_summary: str,
        audit_data: dict[str, Any],
    ) -> tuple[MemoryChange, bool]:
        """Stage a governance record change as a T2 memory change; a maintainer's
        direct change to the applied record applies at once, otherwise it waits for
        a maintainer (``ConfigGate``)."""
        change = service.governance.draft(
            repo,
            after,
            title=title,
            description=description
            + (
                " Builds on the draft that waited, which it replaces."
                if replaced
                else ""
            ),
            requested_by=user.login,
        )
        change.author = user.login
        await service.store.save_memory_change(change)
        await service.store.add_audit(
            AuditEntry(
                kind="config",
                actor=user.login,
                summary=audit_summary,
                data={"change": change.id, **audit_data},
            )
        )
        await service.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                repo,
                MemoryChangePayload(
                    change_id=change.id, replaces=replaced.id if replaced else None
                ),
                discriminator=change.id,
            )
        )
        await service.actuator.drain()
        state = None
        if replaced is None:
            decision = await service.gate.check(repo, user.login, profile, person=True)
            if decision.allowed:
                _, state = await service.approve_memory_change(change.id, user.login)
        stored = await service.store.get_memory_change(change.id)
        applied = (stored.state if stored else state) == MemoryChangeState.MERGED
        return change, applied

    @r.post("/{repo:path}/governance/members")
    async def add(
        request: Request, repo: str, body: AddBody, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Add people to the record, each with a role (and areas, for a role held by
        area). It builds on the draft that waits, as a new draft that replaces it,
        else on the record; a maintainer's addition to the record applies at once,
        anyone else's waits for a maintainer (``ConfigGate``)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        loaded, base, replaced = await base_record(
            service.governance,
            repo,
            await service.store.list_memory_changes(MemoryChangeState.OPEN),
        )
        if loaded.error and replaced is None:
            raise HTTPException(
                409, f"the stored record does not read ({loaded.error}); fix it first"
            )
        today = service.clock.now().date()
        listed = {m.login.lower(): m for m in base.members}
        members = list(base.members)
        added: list[str] = []
        for person in body.people:
            try:
                login = parse_person(person.login).name
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            role = base.role(person.role)
            if role is None:
                raise HTTPException(422, f"the record has no role {person.role!r}")
            if login.lower() in listed:
                held = base.role(listed[login.lower()].role)
                raise HTTPException(
                    422,
                    f"@{login} is listed already, as "
                    f"{held.name if held else listed[login.lower()].role}; "
                    "open their entry to change their role",
                )
            areas = [a.strip() for a in person.areas if a.strip()]
            members.append(
                Member(
                    login=login,
                    role=role.id,
                    areas=areas if role.scoped else [],
                    since=today,
                    note=redact_secrets(person.note.strip())[0],
                )
            )
            listed[login.lower()] = members[-1]
            added.append(login)
        after = revised(
            base,
            base.model_copy(update={"members": members}),
            repo=repo,
            questions=[],
            by=user.login,
            on=today,
            reason="added on the People tab",
        )
        names = ", ".join(f"@{x}" for x in added[:5]) + (
            f" and {len(added) - 5} more" if len(added) > 5 else ""
        )
        change, applied = await _submit_record(
            service,
            user,
            repo,
            profile,
            after,
            replaced,
            title=f"people: add {names} to {repo}",
            description=f"Added on the People tab by {user.login}: "
            + "; ".join(f"@{m.login} as {m.role}" for m in members if m.login in added)
            + ".",
            audit_summary=f"people: added {len(added)} to {repo}",
            audit_data={"added": added},
        )
        return {
            "change": change.id,
            "added": added,
            "applied": applied,
            "on_draft": replaced is not None,
        }

    @r.patch("/{repo:path}/governance/members/{login}")
    async def update_member(
        request: Request,
        repo: str,
        login: str,
        body: UpdateMemberBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Change a listed member's role, areas, or note directly on the People tab.
        A maintainer's change to the applied record applies at once and records the
        change in history; anyone else's waits for a maintainer (``ConfigGate``)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        loaded, base, replaced = await base_record(
            service.governance,
            repo,
            await service.store.list_memory_changes(MemoryChangeState.OPEN),
        )
        if loaded.error and replaced is None:
            raise HTTPException(
                409, f"the stored record does not read ({loaded.error}); fix it first"
            )
        role = base.role(body.role)
        if role is None:
            raise HTTPException(422, f"the record has no role {body.role!r}")
        key = login.strip().lstrip("@").lower()
        idx = next(
            (i for i, m in enumerate(base.members) if m.login.lower() == key), None
        )
        if idx is None:
            raise HTTPException(404, f"@{login} is not listed in {repo}'s record")
        old = base.members[idx]
        today = service.clock.now().date()
        areas = (
            [a.strip() for a in body.areas if a.strip()]
            if body.areas is not None
            else list(old.areas)
        )
        note = (
            redact_secrets(body.note.strip())[0] if body.note is not None else old.note
        )
        updated = old.model_copy(
            update={
                "role": role.id,
                "areas": areas if role.scoped else [],
                "since": today if role.id != old.role else old.since,
                "note": note,
            }
        )
        members = list(base.members)
        members[idx] = updated
        reason = redact_secrets(body.reason.strip())[0] or "updated on the People tab"
        after = revised(
            base,
            base.model_copy(update={"members": members}),
            repo=repo,
            questions=[],
            by=user.login,
            on=today,
            reason=reason,
        )
        change, applied = await _submit_record(
            service,
            user,
            repo,
            profile,
            after,
            replaced,
            title=f"people: @{old.login} → {role.name} in {repo}"
            if role.id != old.role
            else f"people: update @{old.login} in {repo}",
            description=f"Updated on the People tab by {user.login}: "
            f"@{old.login} {old.role} → {role.id}"
            + (f" ({reason})" if body.reason.strip() else "")
            + ".",
            audit_summary=f"people: updated @{old.login} ({old.role} -> {role.id}) in {repo}",
            audit_data={"login": old.login, "from": old.role, "to": role.id},
        )
        return {
            "change": change.id,
            "login": old.login,
            "role": role.id,
            "applied": applied,
            "on_draft": replaced is not None,
        }

    @r.delete("/{repo:path}/governance/members/{login}")
    async def remove_member(
        request: Request,
        repo: str,
        login: str,
        reason: str = Query(default="", max_length=500),
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Remove a member from the People record. A maintainer's removal from the
        applied record applies at once and records it in history; anyone else's
        waits for a maintainer (``ConfigGate``)."""
        service = service_of(request)
        repo = _repo(repo)
        await require_view(service, user, repo)
        profile = await service.profiles.active(repo)
        if profile is None:
            raise HTTPException(409, f"{repo} has no approved profile yet")
        loaded, base, replaced = await base_record(
            service.governance,
            repo,
            await service.store.list_memory_changes(MemoryChangeState.OPEN),
        )
        if loaded.error and replaced is None:
            raise HTTPException(
                409, f"the stored record does not read ({loaded.error}); fix it first"
            )
        key = login.strip().lstrip("@").lower()
        old = next((m for m in base.members if m.login.lower() == key), None)
        if old is None:
            raise HTTPException(404, f"@{login} is not listed in {repo}'s record")
        today = service.clock.now().date()
        members = [m for m in base.members if m.login.lower() != key]
        why = redact_secrets(reason.strip())[0] or "removed on the People tab"
        after = revised(
            base,
            base.model_copy(update={"members": members}),
            repo=repo,
            questions=[],
            by=user.login,
            on=today,
            reason=why,
        )
        change, applied = await _submit_record(
            service,
            user,
            repo,
            profile,
            after,
            replaced,
            title=f"people: remove @{old.login} from {repo}",
            description=f"Removed on the People tab by {user.login}: "
            f"@{old.login} (was {old.role})"
            + (f" ({why})" if reason.strip() else "")
            + ".",
            audit_summary=f"people: removed @{old.login} from {repo}",
            audit_data={"login": old.login, "from": old.role},
        )
        return {
            "change": change.id,
            "login": old.login,
            "applied": applied,
            "on_draft": replaced is not None,
        }

    @r.post("/{repo:path}/governance/agent")
    async def ask(
        request: Request,
        repo: str,
        body: AskBody,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Have the agent fill in or change the record (``edit``), or check who should
        change role (``members``). It drafts a memory change; nothing applies until a
        maintainer approves it."""
        service = service_of(request)
        repo = _repo(repo)
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
