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

"""Reading, drafting, and checking a repository's governance record (design 2.6).

The record is ``repos/<owner>/<repo>/governance.yaml`` in the memory repo. A
repository without one shows the skeleton from ``config/defaults/governance.yaml``.
A change is a T2 memory change for that repository, so it merges only once one of
its maintainers approves it (design 2.3); this module drafts it and never approves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from ..domain.governance import GOVERNANCE_FILE, Governance
from ..domain.memory import MemoryChange, Tier, repo_dir
from ..domain.permissions import Permission
from ..domain.ports import MemoryStore


def governance_path(repo: str) -> str:
    return f"{repo_dir(repo)}/{GOVERNANCE_FILE}"


def render(gov: Governance) -> str:
    """The record as it is stored: YAML in the schema's field order."""
    return yaml.safe_dump(gov.dump(), sort_keys=False, allow_unicode=True, width=100)


@dataclass
class Loaded:
    """A repository's record as the service reads it now."""

    governance: Governance
    stored: bool
    """Whether the memory repo has a record; otherwise ``governance`` is the skeleton."""
    error: str | None = None
    """Why the stored record could not be read; the skeleton stands in for it."""


@dataclass
class Finding:
    """Where the record and the repository disagree."""

    kind: str
    login: str | None
    text: str


@dataclass
class GovernanceRecords:
    """Each repository's record in the memory repo, and the skeleton it starts from."""

    memory: MemoryStore
    skeleton_data: dict[str, Any] = field(default_factory=dict)

    def skeleton(self, repo: str) -> Governance:
        return Governance.model_validate({**self.skeleton_data, "repo": repo})

    async def load(self, repo: str) -> Loaded:
        text = await self.memory.read_text(governance_path(repo))
        if not text:
            return Loaded(self.skeleton(repo), stored=False)
        try:
            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict):
                raise ValueError("the record is not a mapping")
            gov = Governance.model_validate({**data, "repo": repo})
        except (yaml.YAMLError, ValidationError, ValueError) as e:
            return Loaded(self.skeleton(repo), stored=True, error=_short(e))
        return Loaded(gov, stored=True)

    async def maintainers_of(self, repo: str) -> set[str]:
        """Who maintains ``repo`` by its applied record; empty when the record names
        nobody (then its GitHub admins do, design 2.3)."""
        loaded = await self.load(repo)
        return (
            set()
            if loaded.error or not loaded.stored
            else loaded.governance.maintainers()
        )

    async def members_of(self, repo: str) -> set[str]:
        """Everyone ``repo``'s applied record lists."""
        loaded = await self.load(repo)
        return (
            set() if loaded.error or not loaded.stored else loaded.governance.listed()
        )

    def drafted(self, repo: str, change: MemoryChange) -> Governance | None:
        """The record ``change`` would store for ``repo``, or ``None`` when it does
        not change the record or does not read."""
        text = change.files.get(governance_path(repo))
        if not text:
            return None
        try:
            data = yaml.safe_load(text) or {}
            return Governance.model_validate({**data, "repo": repo})
        except (yaml.YAMLError, ValidationError, ValueError, TypeError):
            return None

    def draft(
        self,
        repo: str,
        gov: Governance,
        *,
        title: str,
        description: str,
        requested_by: str | None,
    ) -> MemoryChange:
        """A memory change that replaces the record with ``gov``. It carries no
        approval: a maintainer of ``repo`` approves it after reading it."""
        gov = gov.model_copy(update={"repo": repo})
        return MemoryChange(
            tier=Tier.T2,
            title=title[:120],
            description=description,
            files={governance_path(repo): render(gov)},
            repos=[repo],
            requested_by=requested_by,
        )


def check(
    gov: Governance,
    permissions: dict[str, Permission],
    codeowners: set[str],
) -> list[Finding]:
    """Where the record and GitHub disagree: a member whose permission is below what
    their role expects, a member GitHub does not know on the repository, or a code
    owner the record does not list. ``permissions`` holds each member's live
    permission; ``codeowners`` the logins ``CODEOWNERS`` names."""
    findings: list[Finding] = []
    for m in gov.members:
        role = gov.role(m.role)
        have = permissions.get(m.login.lower())
        if have is None:
            continue
        if have == Permission.NONE:
            findings.append(
                Finding(
                    "no_access",
                    m.login,
                    f"@{m.login} is listed as {role.name if role else m.role} but has "
                    "no access to the repository on GitHub.",
                )
            )
        elif (
            role
            and role.github_permission
            and not have.at_least(role.github_permission)
        ):
            findings.append(
                Finding(
                    "permission",
                    m.login,
                    f"@{m.login} is {role.name}, which expects {role.github_permission} "
                    f"on GitHub, but has {have}.",
                )
            )
    listed = {m.login.lower() for m in gov.members}
    for login in sorted(codeowners - listed):
        findings.append(
            Finding(
                "codeowner",
                login,
                f"@{login} is a code owner but holds no role in the record.",
            )
        )
    return findings


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'record'}: {err['msg']}"
            for err in e.errors()[:5]
        )
    return str(e)[:500]
