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

"""What actuator handlers run with, and how public text is assembled."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.actions import Action, LabReproducePayload, LabRunPayload
from ..domain.clock import Clock
from ..domain.codeowners import codeowners_locations, owners_of_paths, parse_codeowners
from ..domain.labs import LabProgress
from ..domain.markers import build_marker
from ..domain.ports import (
    BranchPublisher,
    CiProvider,
    EventSink,
    ForgeReader,
    ForgeWriter,
    MemoryStore,
    Notifier,
    ServiceUpdater,
    Store,
    Workspace,
)
from ..domain.profile import LabBinding, RepoProfile
from ..domain.snapshot import PrSnapshot
from ..domain.visibility import PublicText
from ..policy.leak_guard import TIER_BLIND, make_public
from ..profiles.knowledge import Knowledge
from ..profiles.service import ProfileService

RUNTIME_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "opencode": "OpenCode",
    "gemini": "Gemini CLI",
    "pi": "pi",
    "dsh": "dsh",
}


class LabRunner(Protocol):
    async def run(
        self,
        profile: RepoProfile,
        payload: LabRunPayload,
        repo: str,
        number: int | None,
        sha: str | None,
        holder: str,
    ) -> dict[str, Any]: ...

    async def open_session(
        self,
        payload: LabReproducePayload,
        repo: str,
        number: int,
        code: dict[str, str],
        holder: str,
        binding: LabBinding | None = None,
        progress: Callable[[LabProgress], Awaitable[None]] | None = None,
        job: str | None = None,
    ) -> dict[str, Any]: ...

    def session_token(self, session_id: str) -> str | None: ...

    def session_info(self, token: str) -> dict[str, Any] | None: ...

    def allow_sync(self, session_id: str, source: str) -> None: ...

    async def close_session(self, session_id: str) -> dict[str, Any] | None: ...

    async def check(self, lab: str) -> dict[str, Any]: ...


@dataclass
class ExecContext:
    store: Store
    reader: ForgeReader
    writer: ForgeWriter
    ci: CiProvider
    memory: MemoryStore
    notifier: Notifier
    knowledge: Knowledge
    profiles: ProfileService
    bus: EventSink
    clock: Clock
    service_login: str
    labs: LabRunner | None = None
    code_repo: str | None = None
    channels: dict[str, str] | None = None
    code_branch: str = "main"
    publisher: BranchPublisher | None = None
    """Pushes self-improvement branches to the code repo."""
    updater: ServiceUpdater | None = None
    """Moves the service's own checkout to a new commit."""
    request_restart: Callable[[], None] | None = None
    """Asks ``am serve`` to exit for its supervisor to start the new code."""
    workspace: Workspace | None = None
    """Checks out a PR to copy its code to a test machine."""
    writer_as: Callable[[str], Awaitable[ForgeWriter | None]] | None = None
    """A forge writer acting as a signed-in maintainer, for review drafts they chose
    to post as themselves; ``None`` when their sign-in no longer works."""
    publisher_as: Callable[[str], Awaitable[BranchPublisher | None]] | None = None
    """A branch publisher pushing as a signed-in maintainer, for fix PRs."""
    memory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lab_progress: (
        Callable[[str, str, int | None, str, LabProgress], Awaitable[None]] | None
    ) = None
    """Shows what a run on a test machine is doing on the job that asked for it:
    (job id, repository, PR, machine, progress)."""
    """Held while the memory gate stages or merges a change: an approval on the
    dashboard and the action that saved the change can reach the gate at once."""

    async def public(
        self,
        action: Action,
        profile: RepoProfile,
        body: str,
        *,
        marker_kind: str,
        pr: PrSnapshot | None,
        runtime: str | None = None,
        approver: str | None = None,
        bare: bool = False,
    ) -> PublicText:
        """Wrap ``body`` with the marker and footer and pass it through the leak guard.

        ``bare`` skips the marker and footer, for inline review comments and merge
        messages.
        """
        if bare:
            text = body.strip()
        else:
            marker = build_marker(
                marker_kind, action.number, action.head_sha, action.id
            )
            name = RUNTIME_NAMES.get(runtime or "", runtime or "a coding agent")
            approval = f", approved by {approver}" if approver else ""
            footer = profile.comments.footer.format(runtime=name, approval=approval)
            text = f"{marker}\n\n{body.strip()}\n\n<sub>{footer}</sub>"
        allowed: set[str] = set()
        if pr is not None:
            allowed.add(pr.author)
            allowed.update(await self.code_owners(profile, pr.paths))
        entities = await self.knowledge.internal_entities(profile.repo, profile)
        public, _warnings = make_public(
            text, profile, allowed, entities, tiers=action.kind not in TIER_BLIND
        )
        return public

    async def code_owners(self, profile: RepoProfile, paths: list[str]) -> list[str]:
        locations = (
            [profile.people.codeowners]
            if profile.people.codeowners
            else list(codeowners_locations(profile.repo))
        )
        for location in locations:
            text = await self.reader.get_file(profile.repo, location)
            if text:
                return owners_of_paths(parse_codeowners(text), paths)
        return []
