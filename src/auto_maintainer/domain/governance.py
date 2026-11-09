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

"""A repository's people: the roles it has, what each is responsible for, how people
move between roles, who holds each role, and how that changed (design 2.6).

Every repository runs this differently, so the model is a skeleton its maintainers
fill in: ``config/defaults/governance.yaml`` is the starting point, and an agent
rewrites it from the repository's own governance documents and what its maintainers
say. The record lives in the memory repo and changes only through a memory change a
maintainer approves. It describes people; it grants nothing in the service, where a
person's GitHub permission stays the only authority (design 2.3).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .permissions import Permission

GOVERNANCE_FILE = "governance.yaml"
"""The record's file in the repository's directory of the memory repo."""

_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Role(_Model):
    """One level of responsibility, such as reviewer, maintainer, or owner."""

    id: str = Field(description="A short lowercase slug, such as `reviewer`.")
    name: str = Field(description="The role's name as the repository writes it.")
    level: int = Field(
        ge=0, description="Rank: higher means more responsibility; roles may share one."
    )
    description: str = ""
    responsibilities: list[str] = Field(
        default_factory=list, description="What a holder is expected to do."
    )
    privileges: list[str] = Field(
        default_factory=list,
        description="What a holder may decide or do in the repository.",
    )
    github_permission: Permission | None = Field(
        default=None,
        description="The GitHub permission a holder is expected to have, if any.",
    )
    scoped: bool = Field(
        default=False,
        description="Whether the role is held for areas (paths or components) "
        "rather than for the whole repository.",
    )


class Criterion(_Model):
    """One condition of a change of role, in the repository's words, with a measure
    an agent can check from GitHub activity when there is one."""

    text: str
    measure: (
        Literal[
            "merged_prs",
            "reviews",
            "months_in_role",
            "inactive_months",
            "nominations",
        ]
        | None
    ) = None
    threshold: float | None = None
    window_months: int | None = Field(
        default=None, description="The period the measure counts over, if limited."
    )


class Transition(_Model):
    """How people move from one role to another, or join or leave."""

    id: str
    from_role: str | None = Field(
        default=None, description="The role left; null for someone joining."
    )
    to_role: str | None = Field(
        default=None, description="The role taken; null for someone leaving."
    )
    criteria: list[Criterion] = Field(default_factory=list)
    process: list[str] = Field(
        default_factory=list, description="The steps, in order, as the process says."
    )
    decided_by: list[str] = Field(
        default_factory=list,
        description="The ids of the roles whose holders decide the change.",
    )
    approvals: int = Field(default=1, ge=1, description="How many of them must agree.")


class Member(_Model):
    login: str
    role: str
    areas: list[str] = Field(
        default_factory=list,
        description="For a scoped role: the paths or components held.",
    )
    since: date | None = None
    score: float | None = Field(
        default=None,
        description="The member's score under the repository's process, when it "
        "keeps one (the record's `score_label` names it).",
    )
    next_step: str = Field(
        default="",
        description="What the member needs for their next role, in one sentence; "
        "empty at the top or when nothing applies.",
    )
    note: str = Field(
        default="", description="Anything else about the member, briefly."
    )


class RoleChange(_Model):
    """One change of someone's role, kept as history."""

    login: str
    from_role: str | None = None
    to_role: str | None = None
    on: date
    by: str = Field(description="Who decided or asked for the change.")
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class Source(_Model):
    """Where the repository writes down its process."""

    kind: Literal["file", "url", "conversation"]
    where: str = Field(description="A path in the repository, a URL, or who said it.")
    note: str = ""


class Governance(_Model):
    """A repository's roles, members, and how people change roles."""

    schema_version: Literal[1] = Field(default=1, alias="schema")
    repo: str = ""
    summary: str = ""
    established: bool = Field(
        default=False,
        description="False while this is still the skeleton nobody has filled in.",
    )
    sources: list[Source] = Field(default_factory=list)
    score_label: str = Field(
        default="",
        description="What the process calls a member's score, when it keeps one.",
    )
    roles: list[Role] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    members: list[Member] = Field(default_factory=list)
    history: list[RoleChange] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _consistent(self) -> Governance:
        problems = problems_of(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def role(self, role_id: str | None) -> Role | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def roles_of(self, login: str) -> list[Member]:
        return [m for m in self.members if m.login.lower() == login.lower()]

    def dump(self) -> dict:
        """The record as stored: ``schema`` first, nothing left at its default
        except what a reader needs."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def problems_of(gov: Governance) -> list[str]:
    """What makes ``gov`` inconsistent: unknown roles, duplicate ids, bad logins."""
    problems: list[str] = []
    ids = [r.id for r in gov.roles]
    for role_id in ids:
        if not _ID.match(role_id):
            problems.append(f"role id {role_id!r} must be a lowercase slug")
    if len(set(ids)) != len(ids):
        problems.append("role ids must be unique")
    known = set(ids)
    seen_transitions: set[str] = set()
    for t in gov.transitions:
        if t.id in seen_transitions:
            problems.append(f"transition id {t.id!r} is used twice")
        seen_transitions.add(t.id)
        for end in (t.from_role, t.to_role):
            if end is not None and end not in known:
                problems.append(f"transition {t.id!r} names unknown role {end!r}")
        if t.from_role is None and t.to_role is None:
            problems.append(f"transition {t.id!r} needs a from_role or a to_role")
        for decider in t.decided_by:
            if decider not in known:
                problems.append(
                    f"transition {t.id!r} is decided by unknown role {decider!r}"
                )
    held: set[tuple[str, str]] = set()
    for m in gov.members:
        if not _LOGIN.match(m.login):
            problems.append(f"{m.login!r} is not a GitHub login")
        if m.role not in known:
            problems.append(f"@{m.login} holds unknown role {m.role!r}")
        key = (m.login.lower(), m.role)
        if key in held:
            problems.append(f"@{m.login} is listed twice as {m.role}")
        held.add(key)
    for c in gov.history:
        for end in (c.from_role, c.to_role):
            if end is not None and end not in known:
                problems.append(f"history of @{c.login} names unknown role {end!r}")
    return problems


def role_changes(
    before: Governance, after: Governance
) -> list[tuple[str, str | None, str | None]]:
    """Who changed role from ``before`` to ``after``: (login, old, new), where a
    ``None`` side means not holding a role there. A login with several roles is
    compared role by role."""

    def held(gov: Governance) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for m in gov.members:
            out.setdefault(m.login.lower(), set()).add(m.role)
        return out

    names = {m.login.lower(): m.login for m in [*before.members, *after.members]}
    old, new = held(before), held(after)
    changes: list[tuple[str, str | None, str | None]] = []
    for key in sorted(set(old) | set(new)):
        gone = sorted(old.get(key, set()) - new.get(key, set()))
        came = sorted(new.get(key, set()) - old.get(key, set()))
        level = {r.id: r.level for r in [*before.roles, *after.roles]}
        gone.sort(key=lambda r: level.get(r, 0))
        came.sort(key=lambda r: level.get(r, 0))
        while gone or came:
            changes.append(
                (
                    names[key],
                    gone.pop(0) if gone else None,
                    came.pop(0) if came else None,
                )
            )
    return changes


def with_history(
    before: Governance, after: Governance, *, by: str, on: date, reason: str
) -> Governance:
    """``after`` with ``before``'s history kept as it was, and a history entry for
    every change of role from ``before`` that the new entries do not record yet, so
    history only grows and no change of someone's role goes unrecorded, whoever
    drafted it."""
    kept = list(before.history)
    new = [c for c in after.history if c not in kept]
    recorded = {(c.login.lower(), c.from_role, c.to_role) for c in new}
    added = [
        RoleChange(
            login=login, from_role=old, to_role=role, on=on, by=by, reason=reason
        )
        for login, old, role in role_changes(before, after)
        if (login.lower(), old, role) not in recorded
    ]
    return after.model_copy(update={"history": [*kept, *new, *added]})
