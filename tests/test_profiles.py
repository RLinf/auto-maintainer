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

"""Profile layering, packs, locks, and the configuration gate (design 2.2, 2.3)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from auto_maintainer.domain.actions import Autonomy
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.memory import (
    MemoryChange,
    MemoryChangeState,
    Tier,
    lock_path,
    profile_path,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.profiles.gate import memory_change_ready
from auto_maintainer.profiles.layers import (
    Layer,
    LayerKind,
    ProfileError,
    build_profile,
    dump_yaml,
)
from auto_maintainer.profiles.loader import IN_REPO_PATH
from auto_maintainer.profiles.lock import parse_lock
from auto_maintainer.profiles.packs import PackLibrary
from auto_maintainer.resources import data_dir
from auto_maintainer.testing.fakes import make_pr
from auto_maintainer.testing.harness import Harness, load_defaults

REPO = "acme/widget"


def _layers(*extra: Layer) -> list[Layer]:
    defaults, _, _ = load_defaults()
    return [Layer(LayerKind.DEFAULTS, "defaults", defaults), *extra]


def test_defaults_keep_repository_actions_in_shadow() -> None:
    profile = build_profile(REPO, _layers()).profile
    assert profile.access.configure == Permission.MAINTAIN
    for kind, rule in profile.policy.items():
        if kind.touches_repository:
            assert rule.level == Autonomy.SHADOW, kind
    assert profile.rule("notify").level == Autonomy.AUTO


def test_layer_kinds_may_not_set_forbidden_keys() -> None:
    with pytest.raises(ProfileError, match="may not set"):
        build_profile(
            REPO,
            _layers(
                Layer(
                    LayerKind.PACK,
                    "pack:x",
                    {"policy": {"pr.merge": {"level": "auto"}}},
                )
            ),
        )
    with pytest.raises(ProfileError, match="may not set"):
        build_profile(
            REPO,
            _layers(
                Layer(LayerKind.IN_REPO, "in_repo", {"priority": {"enabled": False}})
            ),
        )
    with pytest.raises(ProfileError, match="may not set"):
        build_profile(
            REPO,
            _layers(
                Layer(LayerKind.MEMORY, "memory", {"ceilings": {"pr.merge": "auto"}})
            ),
        )
    with pytest.raises(ProfileError, match="host overrides"):
        build_profile(
            REPO, _layers(Layer(LayerKind.HOST, "host", {"merge": {"enabled": True}}))
        )
    with pytest.raises(ProfileError, match="lab paths|may only set"):
        build_profile(
            REPO,
            _layers(
                Layer(LayerKind.HOST, "host", {"labs": {"gpu": {"templates": {}}}})
            ),
        )


def test_in_repo_ceiling_caps_memory_policy() -> None:
    profile = build_profile(
        REPO,
        _layers(
            Layer(
                LayerKind.IN_REPO, "in_repo", {"ceilings": {"review.post": "propose"}}
            ),
            Layer(
                LayerKind.MEMORY,
                "memory",
                {"policy": {"review.post": {"level": "auto"}}},
            ),
        ),
    ).profile
    assert profile.rule("review.post").level == Autonomy.PROPOSE


def test_access_floor_is_write() -> None:
    with pytest.raises(ProfileError, match="write or higher"):
        build_profile(
            REPO,
            _layers(
                Layer(LayerKind.MEMORY, "memory", {"access": {"configure": "read"}})
            ),
        )
    profile = build_profile(
        REPO,
        _layers(Layer(LayerKind.MEMORY, "memory", {"access": {"configure": "admin"}})),
    ).profile
    assert profile.access.configure == Permission.ADMIN


def test_additive_keys_accumulate_and_packs_resolve() -> None:
    packs = PackLibrary.from_dirs(data_dir("packs"))
    layers = _layers(
        *packs.layers_for(["conventional-commits", "dco", "github-actions"]),
        Layer(
            LayerKind.MEMORY,
            "memory",
            {
                "checks": [
                    {"id": "signoff", "blocking": False},
                    {"id": "extra", "name": "paired_paths"},
                ]
            },
        ),
    )
    profile = build_profile(REPO, layers).profile
    ids = [c.id for c in profile.checks]
    assert ids[:3] == ["pr-title", "commit-subjects", "signoff"] and "extra" in ids
    assert next(c for c in profile.checks if c.id == "signoff").blocking is False
    assert profile.contribution.signoff and profile.merge.message.preserve_trailers == [
        "Signed-off-by",
        "Co-authored-by",
    ]
    assert any(s.id == "gha-network-reset" for s in profile.ci.flake_signatures)


def test_gpu_lab_wildcard_applies_to_every_lab() -> None:
    packs = PackLibrary.from_dirs(data_dir("packs"))
    layers = _layers(
        *packs.layers_for(["gpu-lab"]),
        Layer(
            LayerKind.MEMORY, "memory", {"labs": {"gpu-host": {"base_clone": "/srv/x"}}}
        ),
    )
    profile = build_profile(REPO, layers).profile
    assert any("ray" in f for f in profile.labs["gpu-host"].guard.forbid)
    assert "*" not in profile.labs


def test_unknown_pack_and_unknown_role_fail() -> None:
    packs = PackLibrary.from_dirs(data_dir("packs"))
    with pytest.raises(ProfileError, match="unknown pack"):
        packs.layers_for(["nope"])
    with pytest.raises(ProfileError, match="unknown label roles"):
        build_profile(
            REPO,
            _layers(
                Layer(
                    LayerKind.MEMORY, "memory", {"labels": {"roles": {"urgent": "x"}}}
                )
            ),
        )


def test_memory_gate_tiers() -> None:
    from auto_maintainer.domain.clock import utc_now

    now = utc_now()
    t2 = MemoryChange(tier=Tier.T2, title="t", files={}, repos=[REPO], created_at=now)
    assert memory_change_ready(t2, {REPO: True}, {REPO: 24}, now)[0] is False
    t2.approvals = {REPO: ["maria"]}
    assert memory_change_ready(t2, {REPO: True}, {REPO: 24}, now)[0] is True

    t1 = MemoryChange(tier=Tier.T1, title="t", files={}, repos=[REPO], created_at=now)
    assert (
        memory_change_ready(t1, {REPO: False}, {REPO: 24}, now + timedelta(days=3))[0]
        is False
    )
    assert (
        memory_change_ready(t1, {REPO: True}, {REPO: 24}, now + timedelta(hours=2))[0]
        is False
    )
    assert (
        memory_change_ready(t1, {REPO: True}, {REPO: 24}, now + timedelta(hours=25))[0]
        is True
    )
    foreign = t1.model_copy(update={"from_own_signals": False})
    assert (
        memory_change_ready(foreign, {REPO: True}, {REPO: 24}, now + timedelta(days=9))[
            0
        ]
        is False
    )

    two = MemoryChange(
        tier=Tier.T2,
        title="t",
        files={},
        repos=[REPO, "acme/other"],
        approvals={REPO: ["maria"]},
        created_at=now,
    )
    assert memory_change_ready(two, {}, {}, now) == (
        False,
        "waiting for a maintainer of acme/other",
    )


async def test_settings_change_by_writer_waits_and_maintainer_applies(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, layer={"extends": ["dco"]})
    try:
        h.forge.set_permission(REPO, "walt", Permission.WRITE)
        h.forge.set_permission(REPO, "op", Permission.NONE)
        layer = {"schema": 1, "extends": ["dco"], "merge": {"enabled": True}}
        _, change = await h.service.profiles.propose_memory_layer(
            REPO, layer, "walt", "enable merging"
        )
        decision = await h.service.gate.check(
            REPO, "walt", await h.service.profiles.active(REPO)
        )
        assert not decision.allowed and "needs maintain" in decision.reason
        await h.store.save_memory_change(change)
        await h.service.actuator.merge_memory(change.id)
        assert (
            await h.store.get_memory_change(change.id)
        ).state == MemoryChangeState.OPEN
        assert (await h.service.profiles.active(REPO)).merge.enabled is False

        decisions, _ = await h.service.approve_memory_change(change.id, "op")
        assert not decisions[0].allowed, (
            "an operator without maintainer rights cannot configure"
        )
        decisions, state = await h.service.approve_memory_change(change.id, "maria")
        assert decisions[0].allowed and state == MemoryChangeState.MERGED
        profile = await h.service.profiles.active(REPO)
        assert profile is not None and profile.merge.enabled is True
        lock = parse_lock(h.memory.files[lock_path(REPO)], "lock")
        assert lock is not None and lock.approved_by == ["maria"]
    finally:
        await h.close()


async def test_service_cannot_approve_its_own_configuration(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, layer={})
    try:
        h.forge.set_permission(REPO, h.forge.login, Permission.ADMIN)
        decision = await h.service.gate.check(
            REPO, h.forge.login, await h.service.profiles.active(REPO)
        )
        assert not decision.allowed
    finally:
        await h.close()


async def test_in_repo_change_follows_only_when_its_author_qualifies(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, layer={})
    try:
        h.forge.put_file(
            REPO, IN_REPO_PATH, dump_yaml({"schema": 1, "scope": {"min_pr": 100}})
        )
        h.forge.path_changers[(REPO, IN_REPO_PATH)] = "walt"
        h.forge.set_permission(REPO, "walt", Permission.WRITE)
        state = await h.service.profiles.refresh(REPO)
        assert (
            state.candidate_hash
            and await h.service.profiles.auto_follow(REPO, state) is None
        )

        h.forge.path_changers[(REPO, IN_REPO_PATH)] = "maria"
        change = await h.service.profiles.auto_follow(REPO, state)
        assert change is not None and change.approvals == {REPO: ["maria"]}
    finally:
        await h.close()


async def test_release_changes_wait_for_approval(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, layer={})
    try:
        before = await h.service.profiles.active(REPO)
        h.service.loader._defaults = {
            **h.service.loader._defaults,
            "scope": {"min_pr": 7},
        }  # a new release
        state = await h.service.profiles.refresh(REPO)
        assert state.candidate_hash is not None
        assert (
            await h.service.profiles.active(REPO)
        ).scope.min_pr == before.scope.min_pr
        assert await h.service.profiles.auto_follow(REPO, state) is None, (
            "only in-repo changes can follow on their own"
        )
        decision, change = await h.service.profiles.approve_candidate(
            REPO, "maria", state.candidate_hash
        )
        assert decision.allowed and change is not None
        assert profile_path(REPO) not in change.files
    finally:
        await h.close()


async def test_adding_a_repo_again_drafts_a_profile_only_when_none_exists(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    try:
        added: list[str] = []

        async def seen(event: Event) -> None:
            if event.kind == EventKind.REPO_ADDED and event.repo:
                added.append(event.repo)

        h.service.bus.subscribe(seen)
        assert await h.service.profiles.lock(REPO) is not None
        assert await h.memory.read_text(profile_path(REPO)) is None
        await h.service.add_repo(REPO, "maria")
        await h.service.bus.drain()
        assert added == [REPO], "onboarding never drafted this repository's profile"

        h.memory.files[profile_path(REPO)] = dump_yaml({"schema": 1})
        await h.service.add_repo(REPO, "maria")
        await h.service.bus.drain()
        assert added == [REPO]
    finally:
        await h.close()


async def test_a_repository_is_read_as_soon_as_its_first_profile_is_approved(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, approve=False)
    try:
        h.forge.put_pr(make_pr(REPO, 7))
        await h.service.add_repo(REPO, "maria")
        assert not (await h.store.get_repo(REPO)).active
        state = await h.service.profiles.refresh(REPO)
        decision, change = await h.service.profiles.approve_candidate(
            REPO, "maria", state.candidate_hash or ""
        )
        assert decision.allowed and change is not None
        await h.store.save_memory_change(change)
        await h.service.submit_memory_change(change)
        await h.service.bus.drain()
        await asyncio.gather(*h.service._polls.values())
        assert [p.number for p in await h.store.list_snapshots(REPO)] == [7], (
            "the PRs appear without waiting for the next scheduled poll"
        )
    finally:
        await h.close()


async def test_two_approvals_at_once_merge_a_change_once(tmp_path: Path) -> None:
    """The action that saved a change and a click on its card can reach the memory
    gate together; the second must see the change merged, not fail and call it a
    conflict."""
    from auto_maintainer.actuator.memory_gate import try_merge_memory
    from auto_maintainer.domain.memory import (
        MemoryChange,
        MemoryChangeState,
        Tier,
        repo_dir,
    )

    h = await Harness.create(tmp_path)
    try:
        change = MemoryChange(
            tier=Tier.T1,
            title="lesson from maria",
            files={f"{repo_dir(REPO)}/knowledge/review_lessons.md": "- a lesson\n"},
            repos=[REPO],
            approvals={REPO: ["maria"]},
        )
        await h.store.save_memory_change(change)
        merged: list[str] = []
        merge = h.memory.merge_change

        async def merge_once(c: MemoryChange) -> str:
            if c.id in merged:
                raise RuntimeError("merge: am/mem/x - not something we can merge")
            merged.append(c.id)
            await asyncio.sleep(0)
            return await merge(c)

        h.memory.merge_change = merge_once
        x = h.service.actuator.x
        await asyncio.gather(try_merge_memory(x, change), try_merge_memory(x, change))
        assert merged == [change.id]
        stored = await h.store.get_memory_change(change.id)
        assert stored is not None and stored.state == MemoryChangeState.MERGED
    finally:
        await h.close()
