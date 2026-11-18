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

"""Who may do what with the service (design 2.7): roles, their permissions, and the
rules that give each person a role in each repository.

A person's role in a repository comes from, in order: a role set for them there or
everywhere (``config/access.yaml``, which the service's owner and its service admins
change); maintaining it (``ConfigGate``: its People record's maintainers, else its
GitHub admins); their role in its People record, through the repository's mapping;
and their access on GitHub. Their permissions are their role's defaults, as the file
changes them for the role and for the person. Nothing here gets around the
configuration gate: what changes a repository's configuration, or schedules its
merges, stays with its maintainers whatever a role says (``GATED``).

Pure rules: ``resolve`` takes the facts the caller read (``Facts``) and the file.
"""

from __future__ import annotations

from enum import StrEnum

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .permissions import Permission

ACCESS_PATH = "config/access.yaml"
"""Where the memory repo keeps who may do what."""


class Role(StrEnum):
    """A person's role in one repository, from most to least."""

    MAINTAINER = "maintainer"
    REVIEWER = "reviewer"
    GUEST_REVIEWER = "guest_reviewer"
    VIEWER = "viewer"
    BLOCKED = "blocked"


RANK = {
    Role.MAINTAINER: 4,
    Role.REVIEWER: 3,
    Role.GUEST_REVIEWER: 2,
    Role.VIEWER: 1,
    Role.BLOCKED: 0,
}

ROLE_NAMES = {
    Role.MAINTAINER: "Maintainer",
    Role.REVIEWER: "Reviewer",
    Role.GUEST_REVIEWER: "Guest reviewer",
    Role.VIEWER: "Viewer",
    Role.BLOCKED: "Blocked",
}


class Grant(StrEnum):
    """One thing a person may do or see (``ABOUT`` says what)."""

    SEE_PRS = "see.prs"
    SEE_SCORES = "see.scores"
    SEE_OTHERS_WORK = "see.others_work"
    SEE_USAGE = "see.usage"
    SEE_AUDIT = "see.audit"
    SEE_INTERNALS = "see.internals"
    USE_AGENTS = "use.agents"
    USE_MODELS = "use.models"
    USE_MACHINES = "use.machines"
    MACHINES_OUTSIDE_PRS = "machines.outside_prs"
    REVIEW_ASSIGNED = "review.assigned"
    REVIEW_ANY = "review.any"
    DISCUSS = "discuss"
    FIX = "fix"
    SCORE = "score"
    PRIORITY = "priority.override"
    MERGE_SCHEDULE = "merge.schedule"
    PROPOSE = "changes.propose"
    IMPROVE = "service.improve"
    OWN_AGENTS = "own.agents"
    OWN_MODELS = "own.models"
    OWN_MACHINES = "own.machines"
    MANAGE_ACCESS = "manage.access"


ABOUT: dict[Grant, str] = {
    Grant.SEE_PRS: "The repository's PRs, the service's reviews of them, and their progress.",
    Grant.SEE_SCORES: "Internal scores: priority, tiers, quality.",
    Grant.SEE_OTHERS_WORK: "Other people's drafts, agent jobs, and conversations.",
    Grant.SEE_USAGE: "Everyone's usage of agents and models, and its cost.",
    Grant.SEE_AUDIT: "The audit log.",
    Grant.SEE_INTERNALS: "How the service is reached and run: the settings file, test machines' addresses, model APIs' endpoints.",
    Grant.USE_AGENTS: "The service's agents, on its own agent accounts.",
    Grant.USE_MODELS: "The service's model APIs.",
    Grant.USE_MACHINES: "The test machines the service offers the repository.",
    Grant.MACHINES_OUTSIDE_PRS: "Run code from outside contributors' PRs on those machines.",
    Grant.REVIEW_ASSIGNED: "Have agents review and follow up the PRs they review or own.",
    Grant.REVIEW_ANY: "Have agents review and follow up any PR.",
    Grant.DISCUSS: "Talk with agents about their work.",
    Grant.FIX: "Have an agent fix a PR's code or its CI.",
    Grant.SCORE: "Have PRs scored again: priority and quality.",
    Grant.PRIORITY: "Override a PR's priority.",
    Grant.MERGE_SCHEDULE: "Schedule merges; only the repository's maintainers, whatever this file says.",
    Grant.PROPOSE: "Propose changes to the repository's settings, which its maintainers decide.",
    Grant.IMPROVE: "Ask for a change to the service's own code, as a self-improvement PR.",
    Grant.OWN_AGENTS: "Sign their own agent accounts in, for their own tasks.",
    Grant.OWN_MODELS: "Add their own model APIs, for their own tasks.",
    Grant.OWN_MACHINES: "Add their own test machines, for their own tasks.",
    Grant.MANAGE_ACCESS: "Change who may do what (this file), but not who its service admins are.",
}
"""What each grant lets a person do, as the People & permissions page says it."""

GATED = frozenset({Grant.MERGE_SCHEDULE})
"""Grants only the repository's maintainers hold, whatever the file says: what they
approve goes through ``ConfigGate`` (design 2.3)."""

GROUPS: list[tuple[str, list[Grant]]] = [
    (
        "Seeing",
        [
            Grant.SEE_PRS,
            Grant.SEE_SCORES,
            Grant.SEE_OTHERS_WORK,
            Grant.SEE_USAGE,
            Grant.SEE_AUDIT,
            Grant.SEE_INTERNALS,
        ],
    ),
    (
        "The service's agents, models, and machines",
        [
            Grant.USE_AGENTS,
            Grant.USE_MODELS,
            Grant.USE_MACHINES,
            Grant.MACHINES_OUTSIDE_PRS,
        ],
    ),
    (
        "PR work",
        [
            Grant.REVIEW_ASSIGNED,
            Grant.REVIEW_ANY,
            Grant.DISCUSS,
            Grant.FIX,
            Grant.SCORE,
            Grant.PRIORITY,
        ],
    ),
    ("Merges and changes", [Grant.MERGE_SCHEDULE, Grant.PROPOSE, Grant.IMPROVE]),
    ("Their own resources", [Grant.OWN_AGENTS, Grant.OWN_MODELS, Grant.OWN_MACHINES]),
    ("Administration", [Grant.MANAGE_ACCESS]),
]
"""Every grant, grouped as the People & permissions page shows them."""

_OWN = {Grant.OWN_AGENTS, Grant.OWN_MODELS, Grant.OWN_MACHINES}
_REVIEWER = {
    Grant.SEE_PRS,
    Grant.SEE_SCORES,
    Grant.SEE_OTHERS_WORK,
    Grant.USE_AGENTS,
    Grant.USE_MODELS,
    Grant.USE_MACHINES,
    Grant.REVIEW_ASSIGNED,
    Grant.REVIEW_ANY,
    Grant.DISCUSS,
    Grant.FIX,
    Grant.SCORE,
    Grant.PROPOSE,
    Grant.IMPROVE,
    *_OWN,
}

DEFAULTS: dict[Role, frozenset[Grant]] = {
    Role.MAINTAINER: frozenset(set(Grant) - {Grant.MANAGE_ACCESS}),
    Role.REVIEWER: frozenset(_REVIEWER),
    # Outside the team: their assigned PRs, with their own agents and machines.
    Role.GUEST_REVIEWER: frozenset(
        {Grant.SEE_PRS, Grant.REVIEW_ASSIGNED, Grant.DISCUSS, Grant.FIX, *_OWN}
    ),
    Role.VIEWER: frozenset({Grant.SEE_PRS}),
    Role.BLOCKED: frozenset(),
}
"""Each role's permissions before ``config/access.yaml`` changes them."""

SERVICE_ADMIN = frozenset(
    {Grant.MANAGE_ACCESS, Grant.SEE_USAGE, Grant.SEE_AUDIT, Grant.SEE_INTERNALS}
)
"""What a service admin holds in every repository they may see, on top of their role
there; the owner holds it too."""

OPERATOR = frozenset({Grant.SEE_INTERNALS, Grant.SEE_AUDIT})
"""What an operator (``ServiceSettings.operators``) holds, on top of their role."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Model):
    """How much of the service a person may use; ``None`` means no limit."""

    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_week: int | None = Field(default=None, ge=0)
    jobs_at_once: int | None = Field(default=None, ge=0)
    agents: list[str] | None = None
    """The service's agents they may use, by name; ``None`` for every one."""
    machines: list[str] | None = None
    """The test machines they may use, by name; ``None`` for every one offered."""

    def over(self, base: Limits) -> Limits:
        """``base`` with what this sets replacing it."""
        return base.model_copy(update=self.model_dump(exclude_unset=True))


class RoleChange(_Model):
    """How the file changes a role's defaults."""

    grants: dict[Grant, bool] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)


class PersonAccess(_Model):
    """What the file sets for one person."""

    role: Role | None = None
    """Their role in every repository; ``blocked`` keeps them out of the service."""
    repos: dict[str, Role] = Field(default_factory=dict)
    """Their role in single repositories, over ``role``."""
    grants: dict[Grant, bool] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    note: str = ""


class RepoAccess(_Model):
    """How one repository's People record roles map to the service's roles."""

    people_roles: dict[str, Role] = Field(default_factory=dict)
    """People record role id to role; an unmapped one goes by ``default_role``."""


class AccessConfig(_Model):
    """``config/access.yaml``."""

    schema_version: int = Field(default=1, alias="schema")
    admins: list[str] = Field(default_factory=list)
    """Service admins: they change this file, except this list, which only the
    service's owner changes."""
    roles: dict[Role, RoleChange] = Field(default_factory=dict)
    people: dict[str, PersonAccess] = Field(default_factory=dict)
    repos: dict[str, RepoAccess] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("people")
    @classmethod
    def _logins_once(cls, value: dict[str, PersonAccess]) -> dict[str, PersonAccess]:
        lowered = [k.lower() for k in value]
        twice = sorted({k for k in lowered if lowered.count(k) > 1})
        if twice:
            raise ValueError(f"people listed twice: {twice}")
        return value

    def person(self, login: str) -> PersonAccess | None:
        me = login.lower()
        return next((p for k, p in self.people.items() if k.lower() == me), None)

    def is_admin(self, login: str) -> bool:
        return login.lower() in {a.lower() for a in self.admins}

    def mapping(self, repo: str) -> dict[str, Role]:
        me = repo.lower()
        found = next((r for k, r in self.repos.items() if k.lower() == me), None)
        return found.people_roles if found else {}

    def blocked(self, login: str, repo: str | None = None) -> bool:
        """Whether the file blocks ``login`` everywhere, or in ``repo``."""
        p = self.person(login)
        if p is None:
            return False
        if repo is not None:
            here = next(
                (r for k, r in p.repos.items() if k.lower() == repo.lower()), None
            )
            if here is not None:
                return here == Role.BLOCKED
        return p.role == Role.BLOCKED


class PeopleRole(_Model):
    """A person's role in a repository's People record, as ``resolve`` reads it."""

    id: str
    name: str
    maintains: bool = False
    github_permission: Permission | None = None


class Facts(_Model):
    """What the caller read about a person and a repository."""

    login: str
    repo: str
    permission: Permission = Permission.NONE
    """Their live permission on the forge."""
    maintains: bool = False
    """``ConfigGate.check`` allows them."""
    people_role: PeopleRole | None = None
    owner: bool = False
    """They own the service (``Service.service_gate``)."""
    operator: bool = False


class ServiceUse(_Model):
    """What of the service's own a person's agent task may run on (``Grant.USE_AGENTS``,
    ``USE_MODELS``, ``Limits.agents``); their own accounts and model APIs they may
    always use."""

    agents: bool = True
    models: bool = True
    agent_names: list[str] | None = None
    """The service's agents they may use; ``None`` for every one."""


class Effective(_Model):
    """A person's role and permissions in one repository, with where each came from."""

    repo: str
    role: Role | None
    """``None``: no role here, so no access."""
    role_from: str
    grants: dict[Grant, str] = Field(default_factory=dict)
    """Each permission they hold, with where it came from."""
    withheld: dict[Grant, str] = Field(default_factory=dict)
    """Permissions the file gives them that a rule withholds, with why."""
    limits: Limits = Field(default_factory=Limits)
    admin: bool = False
    owner: bool = False

    def can(self, grant: Grant) -> bool:
        return grant in self.grants


def default_role(people_role: PeopleRole) -> Role:
    """The role a People record role maps to when the repository names none for it:
    a role that maintains the repository maintains it here too, one with write
    access reviews, and any other is a guest's."""
    if people_role.maintains:
        return Role.MAINTAINER
    if (
        people_role.github_permission is not None
        and people_role.github_permission.at_least(Permission.WRITE)
    ):
        return Role.REVIEWER
    return Role.GUEST_REVIEWER


def _role(config: AccessConfig, facts: Facts) -> tuple[Role | None, str]:
    person = config.person(facts.login)
    if person is not None:
        here = next(
            (r for k, r in person.repos.items() if k.lower() == facts.repo.lower()),
            None,
        )
        if here is not None:
            return here, f"set for them in {facts.repo}"
        if person.role is not None:
            return person.role, "set for them"
    found: list[tuple[Role, str]] = []
    if facts.maintains:
        found.append((Role.MAINTAINER, f"they maintain {facts.repo}"))
    if facts.people_role is not None and facts.permission.at_least(Permission.READ):
        mapped = config.mapping(facts.repo).get(facts.people_role.id)
        role = mapped or default_role(facts.people_role)
        found.append(
            (
                role,
                f"their role {facts.people_role.name} in {facts.repo}'s People record"
                + ("" if mapped else " (the default mapping)"),
            )
        )
    if facts.permission.at_least(Permission.WRITE):
        found.append((Role.REVIEWER, f"write access to {facts.repo}"))
    if not found:
        return (
            None,
            f"neither write access to {facts.repo} nor a place in its People record",
        )
    # Each source may only raise a role: GitHub write access is never less than a
    # reviewer's, whatever the People record says.
    return max(found, key=lambda f: RANK[f[0]])


def resolve(config: AccessConfig, facts: Facts) -> Effective:
    """``facts.login``'s role and permissions in ``facts.repo``."""
    role, role_from = _role(config, facts)
    admin = config.is_admin(facts.login)
    out = Effective(
        repo=facts.repo,
        role=role,
        role_from=role_from,
        admin=admin,
        owner=facts.owner,
    )
    if role is None or role == Role.BLOCKED:
        return out
    change = config.roles.get(role, RoleChange())
    granted: dict[Grant, str] = dict.fromkeys(
        DEFAULTS[role], f"the {ROLE_NAMES[role]} role"
    )
    for grant, on in change.grants.items():
        if on:
            granted[grant] = f"the {ROLE_NAMES[role]} role, as changed for the service"
        else:
            granted.pop(grant, None)
    extras = [
        (facts.owner, SERVICE_ADMIN, "they own the service"),
        (admin, SERVICE_ADMIN, "they are a service admin"),
        (facts.operator, OPERATOR, "they operate the service"),
    ]
    for held, grants, why in extras:
        if held:
            for grant in grants:
                granted.setdefault(grant, why)
    person = config.person(facts.login)
    limits = change.limits
    if person is not None:
        for grant, on in person.grants.items():
            if on:
                granted[grant] = "set for them"
            else:
                granted.pop(grant, None)
        limits = person.limits.over(limits)
    for grant in GATED:
        if grant in granted and not facts.maintains:
            out.withheld[grant] = f"only {facts.repo}'s maintainers"
            del granted[grant]
    if Grant.MANAGE_ACCESS in granted and not (admin or facts.owner):
        out.withheld[Grant.MANAGE_ACCESS] = "only the owner and the service admins"
        del granted[Grant.MANAGE_ACCESS]
    out.grants = granted
    out.limits = limits
    return out


def parse_access(text: str) -> AccessConfig:
    """``config/access.yaml`` from its text; an empty file says nothing.

    Raises:
        ValueError: It does not read, or names a role or permission there is not.
    """
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise ValueError(f"{ACCESS_PATH} is not YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{ACCESS_PATH} is not a mapping")
    try:
        return AccessConfig.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{ACCESS_PATH}: {e}") from e


def dump_access(config: AccessConfig) -> str:
    """``config`` as the text of ``config/access.yaml``: only what it sets."""
    data = config.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data = {"schema": config.schema_version, **data}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
