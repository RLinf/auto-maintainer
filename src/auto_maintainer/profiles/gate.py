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

"""The configuration gate (design 2.3): who may change a repository's configuration.

Every path that changes a profile layer or lock calls ``ConfigGate.check``. A
repository's maintainers are the people its applied People record names in a role
that ``maintains`` (design 2.6), who must still have access to it on the forge; when
the record names nobody, those whose forge permission reaches ``access.configure``
(the repository's admins by default). Permissions are read from the forge at the
moment of the change and never from a cache.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from pydantic import BaseModel

from ..domain.memory import MemoryChange, Tier
from ..domain.permissions import Permission
from ..domain.ports import ForgeReader
from ..domain.profile import RepoProfile


class GateDecision(BaseModel):
    allowed: bool
    repo: str
    login: str
    required: Permission
    actual: Permission
    reason: str


class ConfigGate:
    def __init__(
        self,
        forge: ForgeReader,
        service_identities: set[str],
        maintainers: Callable[[str], Awaitable[set[str]]] | None = None,
    ) -> None:
        self._forge = forge
        self._service = {s.lower() for s in service_identities}
        self._maintainers = maintainers
        """A repository's maintainers by its People record (lowercased logins)."""

    def add_service_identity(self, login: str) -> None:
        self._service.add(login.lower())

    async def check(
        self,
        repo: str,
        login: str,
        current: RepoProfile | None,
        candidate: RepoProfile | None = None,
        *,
        person: bool = False,
    ) -> GateDecision:
        """Whether ``login`` may change ``repo``'s configuration: whether they
        maintain it (design 2.3).

        When the repository's People record names maintainers (members of a role
        that ``maintains``), only they do, while they can still read it on GitHub.
        Otherwise the bar is the approved profile's ``access.configure`` (for a
        repository without one, the candidate's; ``admin`` without either). A change
        that lowers the bar still needs the current bar.

        ``person`` says that ``login`` is someone who signed in to the dashboard or
        ran the CLI as themselves. Only then may it be the service's own account, as
        when the service acts as its maintainer's GitHub CLI login. A login the forge
        reports (who landed a file) or one the service supplies never may: the service
        cannot approve its own changes.
        """
        bars = [p.access.configure for p in (current, candidate) if p is not None]
        required = (
            current.access.configure
            if current
            else (bars[0] if bars else Permission.ADMIN)
        )
        if login.lower() in self._service and not person:
            return GateDecision(
                allowed=False,
                repo=repo,
                login=login,
                required=required,
                actual=Permission.NONE,
                reason="the service cannot approve its own configuration changes",
            )
        actual = await self._forge.get_permission(repo, login)
        named = await self._maintainers(repo) if self._maintainers else set()
        if named:
            allowed = login.lower() in named and actual.at_least(Permission.READ)
            reason = (
                f"{login} maintains {repo} by its People record"
                if allowed
                else f"{login} is not a maintainer of {repo} in its People record"
                if login.lower() not in named
                else f"{login} is a maintainer of {repo} in its People record but "
                "has no access to it on GitHub"
            )
            return GateDecision(
                allowed=allowed,
                repo=repo,
                login=login,
                required=Permission.READ,
                actual=actual,
                reason=reason,
            )
        allowed = actual.at_least(required)
        reason = (
            f"{login} has {actual} on {repo}"
            if allowed
            else f"{login} has {actual} on {repo}; its People record names no "
            f"maintainers, so changing its configuration needs {required}"
        )
        return GateDecision(
            allowed=allowed,
            repo=repo,
            login=login,
            required=required,
            actual=actual,
            reason=reason,
        )

    async def check_service(
        self,
        repo: str | None,
        owner: str | None,
        login: str,
        *,
        person: bool = False,
    ) -> GateDecision:
        """Whether ``login`` may act as the owner of this deployment: change the
        service's settings (agents, providers, routes, system values, secrets), the
        host setup, and the service's own code (design 2.3).

        One person owns a deployment: ``owner``, while they are an admin of the
        service's repository ``repo``, checked live on its forge. A deployment set up
        before owners has none; then every admin of ``repo`` acts as owner until one
        takes ownership. Being an operator of the host grants nothing here, and the
        service never approves its own changes; ``person`` means what it means for
        ``check``.
        """
        required = Permission.ADMIN

        def refuse(reason: str, actual: Permission = Permission.NONE) -> GateDecision:
            return GateDecision(
                allowed=False,
                repo=repo or "",
                login=login,
                required=required,
                actual=actual,
                reason=reason,
            )

        if not repo:
            return refuse(
                "this service has no repository of its own yet; set one on the setup "
                "page (open the link `am setup` prints on the host)"
            )
        if login.lower() in self._service and not person:
            return refuse("the service cannot approve its own configuration changes")
        if owner and login.lower() != owner.lower():
            return refuse(f"only {owner}, who owns this service, may do this")
        actual = await self._forge.get_permission(repo, login)
        if not actual.at_least(required):
            return refuse(
                f"{login} has {actual} on {repo}; the owner of this service must be "
                "an admin of it",
                actual,
            )
        return GateDecision(
            allowed=True,
            repo=repo,
            login=login,
            required=required,
            actual=actual,
            reason=(
                f"{login} owns this service and is an admin of {repo}"
                if owner
                else f"{login} is an admin of {repo}, and nobody has taken ownership yet"
            ),
        )


def memory_change_ready(
    change: MemoryChange,
    knowledge_auto_merge: dict[str, bool],
    notice_hours: dict[str, int],
    now: datetime,
) -> tuple[bool, str]:
    """Whether the memory gate may merge ``change`` now (design 2.3 and 5.11).

    ``change.approvals`` must only contain approvals that passed ``ConfigGate`` when
    they were recorded.
    """
    if change.tier == Tier.T0:
        return True, "journal entries merge directly"
    if change.service:
        if any(change.approvals.values()):
            return True, "approved by the owner of this service"
        return False, "waiting for the owner of this service"
    unapproved = [r for r in change.repos if not change.approvals.get(r)]
    if change.tier == Tier.T2 or not change.repos:
        if not change.repos:
            return (
                False,
                "a change outside any repository directory needs an operator-reviewed code path",
            )
        if unapproved:
            return False, f"waiting for a maintainer of {', '.join(unapproved)}"
        return True, "approved by a maintainer of every affected repository"
    # T1 knowledge
    if not unapproved:
        return True, "approved by a maintainer of every affected repository"
    if not change.from_own_signals:
        return (
            False,
            "knowledge from outside the repository needs a maintainer's approval",
        )
    blocked = [r for r in change.repos if not knowledge_auto_merge.get(r, False)]
    if blocked:
        return False, f"{', '.join(blocked)} requires approval for knowledge changes"
    window = max(notice_hours.get(r, 24) for r in change.repos)
    elapsed = (now - change.created_at).total_seconds() / 3600
    if elapsed < window:
        return False, f"notice window: {window - elapsed:.1f} hours left"
    return True, "notice window elapsed without objection"
