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

Every path that changes a profile layer or lock calls ``ConfigGate.check``. It
reads the user's permission from the forge at the moment of the change and never
from a cache.
"""

from __future__ import annotations

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
    def __init__(self, forge: ForgeReader, service_identities: set[str]) -> None:
        self._forge = forge
        self._service = {s.lower() for s in service_identities}

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
        """Whether ``login`` may change ``repo``'s configuration.

        The bar is the approved profile's ``access.configure``; for a repository
        without one, the candidate's. A change that lowers the bar still needs the
        current bar.

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
            else (bars[0] if bars else Permission.MAINTAIN)
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
        allowed = actual.at_least(required)
        reason = (
            f"{login} has {actual} on {repo}"
            if allowed
            else f"{login} has {actual} on {repo}; changing its configuration needs {required}"
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
        login: str,
        required: Permission,
        *,
        person: bool = False,
    ) -> GateDecision:
        """Whether ``login`` may change the service's own settings (agents, providers,
        default routes, system values, secrets).

        Those belong to the maintainers of the service's repository on the forge,
        checked live, like a repository's configuration. Being an operator of the host
        grants nothing here, and the service never approves its own changes; ``person``
        means what it means for ``check``.
        """
        if not repo:
            return GateDecision(
                allowed=False,
                repo="",
                login=login,
                required=required,
                actual=Permission.NONE,
                reason=(
                    "no repository owns this service's settings: set code_repo (or a "
                    "memory remote) in local.yaml so its maintainers can change them"
                ),
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
        allowed = actual.at_least(required)
        reason = (
            f"{login} has {actual} on {repo}"
            if allowed
            else f"{login} has {actual} on {repo}; changing the service's settings needs {required}"
        )
        return GateDecision(
            allowed=allowed,
            repo=repo,
            login=login,
            required=required,
            actual=actual,
            reason=reason,
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
            return True, "approved by a maintainer of the service's repository"
        return False, "waiting for a maintainer of the service's repository"
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
