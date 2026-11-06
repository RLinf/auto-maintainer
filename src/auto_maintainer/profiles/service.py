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

"""Approved profiles, pending candidates, and the memory changes that move between them.

This module never writes. Approvals produce ``MemoryChange`` objects that callers
submit as ``memory.change`` actions; the actuator stages and merges them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from ..domain.clock import Clock
from ..domain.memory import MemoryChange, Tier, lock_path, profile_path
from ..domain.ports import ForgeReader, MemoryStore, Store
from ..domain.profile import RepoProfile
from ..domain.records import ProfileState
from .gate import ConfigGate, GateDecision
from .layers import MergedProfile, ProfileError, dump_yaml
from .loader import IN_REPO_PATH, ProfileLoader
from .lock import ProfileLock, lock_from, parse_lock, profile_diff


class ProfileService:
    def __init__(
        self,
        loader: ProfileLoader,
        memory: MemoryStore,
        store: Store,
        forge: ForgeReader,
        gate: ConfigGate,
        clock: Clock,
    ) -> None:
        self.loader = loader
        self._memory = memory
        self._store = store
        self._forge = forge
        self.gate = gate
        self._clock = clock
        self._cache: dict[
            str, tuple[str, ProfileLock | None, RepoProfile | None, str | None]
        ] = {}

    def invalidate(self, repo: str | None = None) -> None:
        if repo is None:
            self._cache.clear()
        else:
            self._cache.pop(repo, None)

    async def _load(
        self, repo: str
    ) -> tuple[ProfileLock | None, RepoProfile | None, str | None]:
        head = await self._memory.head()
        cached = self._cache.get(repo)
        if cached and cached[0] == head:
            return cached[1], cached[2], cached[3]
        lock: ProfileLock | None = None
        profile: RepoProfile | None = None
        error: str | None = None
        try:
            lock = parse_lock(
                await self._memory.read_text(lock_path(repo)), lock_path(repo)
            )
            profile = lock.to_profile() if lock else None
        except (ProfileError, ValidationError) as e:
            error = f"the approved lock does not validate: {e}"
            # Keep serving the last good profile, if this process had one.
            if cached and cached[2] is not None:
                lock, profile = cached[1], cached[2]
        self._cache[repo] = (head, lock, profile, error)
        return lock, profile, error

    async def lock(self, repo: str) -> ProfileLock | None:
        return (await self._load(repo))[0]

    async def active(self, repo: str) -> RepoProfile | None:
        """The approved profile, or ``None`` when the repository has no approved lock."""
        return (await self._load(repo))[1]

    async def refresh(self, repo: str) -> ProfileState:
        """Recompute the candidate from current layers and record it next to the lock."""
        lock, _, lock_error = await self._load(repo)
        state = await self._store.get_profile_state(repo) or ProfileState(repo=repo)
        previous_hash = state.candidate_hash
        try:
            merged = await self.loader.candidate(repo)
            error = None
        except ProfileError as e:
            merged, error = None, str(e)
        state.lock_hash = lock.hash if lock else None
        state.candidate_error = error or lock_error
        if merged is None:
            state.candidate = None
            state.candidate_hash = None
            state.candidate_layers = {}
            state.candidate_diff = None
        elif lock and merged.hash == lock.hash:
            state.candidate = None
            state.candidate_hash = None
            state.candidate_layers = {}
            state.candidate_diff = None
            state.approvals = []
        else:
            state.candidate = merged.data
            state.candidate_hash = merged.hash
            state.candidate_layers = merged.layer_hashes
            state.candidate_diff = profile_diff(
                lock.profile if lock else None, merged.data
            )
            if previous_hash != merged.hash:
                state.approvals = []
        state.updated_at = self._clock.now()
        await self._store.save_profile_state(state)
        return state

    def lock_change(
        self,
        merged: MergedProfile,
        approvers: list[str],
        source: str,
        extra_files: dict[str, str | None] | None = None,
        title: str | None = None,
    ) -> MemoryChange:
        """A T2 change that writes ``merged`` as the new lock (plus ``extra_files``)."""
        repo = merged.profile.repo
        lock = lock_from(merged, approvers, self._clock.now(), source)
        files: dict[str, str | None] = {lock_path(repo): lock.to_yaml()}
        files.update(extra_files or {})
        return MemoryChange(
            tier=Tier.T2,
            title=title or f"profile: approve configuration for {repo}",
            description=f"Source: {source}. Layers: {merged.layer_hashes}.",
            files=files,
            repos=[repo],
            approvals={repo: sorted(set(approvers))} if approvers else {},
            created_at=self._clock.now(),
        )

    async def auto_follow(self, repo: str, state: ProfileState) -> MemoryChange | None:
        """Approve a candidate whose only change is the in-repo file, when whoever
        landed that file meets the bar of the current lock."""
        lock, current, _ = await self._load(repo)
        if not lock or not current or not state.candidate:
            return None
        names = set(lock.layers) | set(state.candidate_layers)
        changed = {
            n for n in names if lock.layers.get(n) != state.candidate_layers.get(n)
        }
        if changed != {"in_repo"}:
            return None
        login = await self._forge.path_changed_by(repo, IN_REPO_PATH)
        if not login:
            return None
        candidate = RepoProfile.model_validate(state.candidate)
        decision = await self.gate.check(repo, login, current, candidate)
        if not decision.allowed:
            return None
        merged = await self.loader.candidate(repo)
        if merged.hash != state.candidate_hash:
            return None
        return self.lock_change(
            merged,
            [login],
            "in_repo",
            title=f"profile: follow {IN_REPO_PATH} in {repo}",
        )

    async def approve_candidate(
        self, repo: str, login: str, expected_hash: str, source: str = "dashboard"
    ) -> tuple[GateDecision, MemoryChange | None]:
        """Approve the pending candidate if ``login``, a person on the dashboard or the
        CLI, passes the gate and it is unchanged."""
        lock, current, _ = await self._load(repo)
        merged = await self.loader.candidate(repo)
        decision = await self.gate.check(
            repo, login, current, merged.profile, person=True
        )
        if not decision.allowed:
            return decision, None
        if merged.hash != expected_hash:
            raise ProfileError(
                f"the candidate changed since it was shown (expected {expected_hash}, now {merged.hash}); review it again"
            )
        if lock is None:
            source = "onboarding" if source == "dashboard" else source
        return decision, self.lock_change(merged, [login], source)

    async def propose_memory_layer(
        self, repo: str, layer: dict[str, Any], author: str, title: str
    ) -> tuple[MergedProfile, MemoryChange]:
        """A T2 change that replaces the memory layer and locks the resulting profile.

        The caller gate-checks the author and records the approval if they qualify.
        """
        merged = await self.loader.candidate(repo, memory_override=layer)
        change = self.lock_change(
            merged,
            [],
            "dashboard",
            extra_files={profile_path(repo): dump_yaml(layer)},
            title=title,
        )
        change.author = author
        return merged, change


def finalize_lock_files(change: MemoryChange, now: datetime) -> MemoryChange:
    """Stamp each lock file in ``change`` with the approvals recorded on the change."""
    files = dict(change.files)
    for path, text in change.files.items():
        if text is None or not path.endswith("/profile.lock.yaml"):
            continue
        lock = parse_lock(text, path)
        if lock is None:
            continue
        approvers = sorted(set(change.approvals.get(lock.repo, []) + lock.approved_by))
        files[path] = lock.model_copy(
            update={"approved_by": approvers, "approved_at": now}
        ).to_yaml()
    return change.model_copy(update={"files": files})
