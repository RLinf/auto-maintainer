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

"""The whole service on fakes, for tests, replay evals, and ``am serve --demo``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..domain.clock import FixedClock
from ..domain.events import Event
from ..domain.memory import lock_path, profile_path
from ..domain.permissions import Permission
from ..domain.ports import CommandResult
from ..domain.profile import Route
from ..domain.records import RepoRecord
from ..domain.snapshot import RepoInfo
from ..playbooks.base import Playbook
from ..profiles.layers import dump_yaml
from ..profiles.lock import lock_from
from ..resources import data_dir, defaults_dir
from ..service import Adapters, Service, ServiceSettings
from ..store.sqlite import SqliteStore
from .fakes import (
    ActingAs,
    FakeForge,
    FakeLabRunner,
    FakeMemory,
    FakePublisher,
    FakeSecrets,
    FakeUpdater,
    FakeUserTokens,
    FakeWorkspace,
    Reconfigured,
    RecordingNotifier,
    ScriptedRunner,
)


def _acting_as(forge: FakeForge) -> Any:
    async def writer(login: str, token: Any) -> ActingAs:
        return ActingAs(forge, login)

    return writer


def _pushing_as(publisher: FakePublisher) -> Any:
    async def push(login: str, token: Any) -> FakePublisher:
        return publisher

    return push


DEFAULT_LABELS = [
    "bug",
    "enhancement",
    "documentation",
    "needs review",
    "waiting on author",
    "run-ci",
    "community",
    "reproduced",
]


class FakeSandbox:
    def __init__(self) -> None:
        self.runs: list[list[str]] = []
        self.results: list[CommandResult] = []

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> CommandResult:
        self.runs.append(argv)
        return self.results.pop(0) if self.results else CommandResult(exit_code=0)


def load_defaults() -> tuple[dict[str, Any], dict[str, Route], dict[str, str]]:
    root = defaults_dir()
    profile = yaml.safe_load((root / "profile.yaml").read_text(encoding="utf-8"))
    routing = {
        k: Route.model_validate(v)
        for k, v in yaml.safe_load(
            (root / "routing.yaml").read_text(encoding="utf-8")
        ).items()
    }
    schedules = yaml.safe_load((root / "schedules.yaml").read_text(encoding="utf-8"))
    return profile, routing, schedules


class Harness:
    def __init__(
        self,
        service: Service,
        forge: FakeForge,
        memory: FakeMemory,
        runner: ScriptedRunner,
        clock: FixedClock,
        sandbox: FakeSandbox,
        notifier: RecordingNotifier,
    ) -> None:
        self.service = service
        self.forge = forge
        self.memory = memory
        self.runner = runner
        self.clock = clock
        self.sandbox = sandbox
        self.notifier = notifier
        self.store = service.store

    @classmethod
    async def create(
        cls,
        tmp: Path,
        repo: str = "acme/widget",
        layer: dict[str, Any] | None = None,
        playbooks: list[Playbook] | None = None,
        labels: list[str] | None = None,
        maintainer: str = "maria",
        approve: bool = True,
        dev_mode: bool = False,
        code_repo: str | None = None,
    ) -> Harness:
        forge = FakeForge()
        forge.add_repo(
            RepoInfo(
                name=repo, labels=labels or DEFAULT_LABELS, default_branch_sha="d" * 40
            )
        )
        forge.set_permission(repo, maintainer, Permission.MAINTAIN)
        if code_repo:
            forge.add_repo(RepoInfo(name=code_repo, default_branch_sha="e" * 40))
            forge.set_permission(code_repo, maintainer, Permission.MAINTAIN)
        memory = FakeMemory()
        store = await SqliteStore.open(tmp / "am.db")
        runner = ScriptedRunner()
        clock = FixedClock()
        sandbox = FakeSandbox()
        notifier = RecordingNotifier()
        defaults, routing, schedules = load_defaults()
        publisher = FakePublisher()
        adapters = Adapters(
            forge=forge,
            actuator_reader=forge,
            writer=forge,
            ci=forge,
            memory=memory,
            store=store,
            agents=runner,
            workspace=FakeWorkspace(tmp / "ws"),
            sandbox=sandbox,
            notifier=notifier,
            clock=clock,
            publisher=publisher if code_repo else None,
            updater=FakeUpdater() if code_repo else None,
            secrets=FakeSecrets(),
            reconfigure=Reconfigured(),
            user_tokens=FakeUserTokens(),
            writer_for_user=_acting_as(forge),
            publisher_for_user=_pushing_as(publisher),
            lab_runner=FakeLabRunner(),
        )
        settings = ServiceSettings(
            login=forge.login,
            identities=set(),
            routing=routing,
            defaults_profile=defaults,
            defaults_governance=yaml.safe_load(
                (defaults_dir() / "governance.yaml").read_text(encoding="utf-8")
            ),
            skills_dir=data_dir("skills"),
            scratch_dir=tmp / "scratch",
            pack_dirs=[data_dir("packs")],
            schedules=schedules,
            dev_mode=dev_mode,
            code_repo=code_repo,
            confirm_deploy_after=0.0,
        )
        service = Service(adapters, settings, playbooks)
        h = cls(service, forge, memory, runner, clock, sandbox, notifier)
        if layer is not None:
            memory.files[profile_path(repo)] = dump_yaml({"schema": 1, **layer})
        if approve:
            await h.approve_directly(repo, maintainer)
        return h

    async def approve_directly(self, repo: str, maintainer: str) -> None:
        """Write the current candidate as the lock, as if the maintainer approved it."""
        merged = await self.service.loader.candidate(repo)
        lock = lock_from(merged, [maintainer], self.clock.now(), "test")
        self.memory.files[lock_path(repo)] = lock.to_yaml()
        self.memory._bump("lock")
        self.service.profiles.invalidate(repo)
        record = await self.store.get_repo(repo) or RepoRecord(
            repo=repo, added_by=maintainer, added_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
        record.active = True
        await self.store.save_repo(record)

    async def emit(self, event: Event) -> None:
        await self.service.bus.publish(event)
        await self.settle()

    async def settle(self) -> None:
        await self.service.bus.drain()
        await self.service.engine.run_until_idle()
        await self.service.actuator.drain()
        await self.service.bus.drain()
        await self.service.engine.run_until_idle()

    async def poll(
        self, repo: str = "acme/widget", reconcile: bool = False
    ) -> list[Event]:
        events = await self.service.poller.poll_repo(repo, reconcile)
        await self.settle()
        return events

    async def close(self) -> None:
        await self.service.actuator.stop()
        await self.service.bus.stop()
        await self.store.close()  # type: ignore[attr-defined]
