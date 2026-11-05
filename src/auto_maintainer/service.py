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

"""The service: application components wired over a set of adapters.

This module never imports an adapter. ``app.py`` builds real adapters and
``testing.harness`` builds fakes; both hand them to ``Service``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actuator.context import ExecContext, LabRunner
from .actuator.executor import Actuator
from .actuator.pipeline import ActionPipeline
from .domain.clock import Clock
from .domain.events import Event, EventKind
from .domain.memory import MemoryChange, MemoryChangeState
from .domain.ports import (
    AgentRunner,
    CiProvider,
    ForgeReader,
    ForgeWriter,
    LabHost,
    MemoryStore,
    Notifier,
    Sandbox,
    Store,
    Workspace,
)
from .domain.profile import Route
from .domain.records import AuditEntry, Job, RepoRecord
from .engine.bus import EventBus
from .engine.engine import Engine
from .engine.poller import Poller
from .engine.questions import QuestionDesk
from .engine.scheduler import Scheduler
from .playbooks import all_playbooks
from .playbooks.base import (
    CiReader,
    LabStatus,
    MemoryReader,
    Playbook,
    PlaybookContext,
    ServiceInfo,
)
from .profiles.gate import ConfigGate, GateDecision
from .profiles.knowledge import Knowledge
from .profiles.loader import ProfileLoader
from .profiles.packs import PackLibrary
from .profiles.service import ProfileService

log = logging.getLogger(__name__)


@dataclass
class Adapters:
    forge: ForgeReader
    """Reader for playbooks and the poller (may cache snapshots)."""
    actuator_reader: ForgeReader
    """Uncached reader the actuator uses to refuse stale actions."""
    writer: ForgeWriter
    ci: CiProvider
    memory: MemoryStore
    store: Store
    agents: AgentRunner
    workspace: Workspace
    sandbox: Sandbox
    notifier: Notifier
    clock: Clock
    labs: dict[str, LabHost] = field(default_factory=dict)
    lab_runner: LabRunner | None = None


@dataclass
class ServiceSettings:
    login: str
    identities: set[str]
    routing: dict[str, Route]
    defaults_profile: dict[str, Any]
    skills_dir: Path
    scratch_dir: Path
    pack_dirs: list[Path] = field(default_factory=list)
    extra_packs: dict[str, str] = field(default_factory=dict)
    host_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    schedules: dict[str, str] = field(default_factory=dict)
    pools: dict[str, int] = field(
        default_factory=lambda: {"io": 8, "agent": 3, "lab": 1}
    )
    dev_mode: bool = False
    write_caps: tuple[int, int] = (120, 40)
    code_repo: str | None = None
    poll_seconds: int = 120
    operators: set[str] = field(default_factory=set)
    base_url: str | None = None
    """Where the dashboard is reached, for links in alerts."""
    sandbox_repo: str | None = None
    """In dev mode, the only repository the service serves and may act on."""


class Service:
    def __init__(
        self,
        adapters: Adapters,
        settings: ServiceSettings,
        playbooks: list[Playbook] | None = None,
    ) -> None:
        self.a = adapters
        self.settings = settings
        self.store = adapters.store
        self.clock = adapters.clock
        self.knowledge = Knowledge(adapters.memory)
        self.packs = PackLibrary.from_dirs(
            *settings.pack_dirs, extra=settings.extra_packs
        )
        self.loader = ProfileLoader(
            adapters.forge,
            adapters.memory,
            self.packs,
            settings.defaults_profile,
            settings.host_overrides,
        )
        self.gate = ConfigGate(adapters.forge, settings.identities | {settings.login})
        self.profiles = ProfileService(
            self.loader,
            adapters.memory,
            adapters.store,
            adapters.forge,
            self.gate,
            adapters.clock,
        )
        self.bus = EventBus()
        self.questions = QuestionDesk(
            adapters.store,
            self.bus,
            adapters.notifier,
            adapters.clock,
            channel_for=self._question_channel,
            dashboard_url=settings.base_url,
        )
        adapters.agents.bind_questions(self.questions)
        self.exec = ExecContext(
            store=adapters.store,
            reader=adapters.actuator_reader,
            writer=adapters.writer,
            ci=adapters.ci,
            memory=adapters.memory,
            notifier=adapters.notifier,
            knowledge=self.knowledge,
            profiles=self.profiles,
            bus=self.bus,
            clock=adapters.clock,
            service_login=settings.login,
            labs=adapters.lab_runner,
            code_repo=settings.code_repo,
        )
        self.actuator = Actuator(self.exec)
        self.pipeline = ActionPipeline(
            adapters.store,
            self.profiles,
            self.knowledge,
            self.actuator,
            self.bus,
            adapters.clock,
            dev_mode=settings.dev_mode,
            write_caps=settings.write_caps,
            sandbox_repo=settings.sandbox_repo,
        )
        self.info = ServiceInfo(
            login=settings.login,
            identities=settings.identities | {settings.login},
            routing=settings.routing,
            skills_dir=settings.skills_dir,
            dev_mode=settings.dev_mode,
            pack_skill_dirs=[],
            scratch_dir=settings.scratch_dir,
        )
        self.engine = Engine(
            adapters.store,
            playbooks if playbooks is not None else all_playbooks(),
            self.context_for,
            self.pipeline,
            self.profiles,
            self.bus,
            adapters.clock,
            settings.pools,
            serves=self.serves,
        )
        self.bus.subscribe(self.engine.handle_event)
        self.poller = Poller(
            adapters.forge,
            adapters.store,
            self.bus,
            self.profiles,
            adapters.clock,
            settings.login,
            serves=self.serves,
        )
        self.scheduler = Scheduler(
            settings.schedules,
            self.bus,
            adapters.clock,
            builtins={
                "reconcile": lambda: self.poller.poll_all(reconcile=True),
                "memory_sync": self.memory_sync,
            },
            every_minute=[self.engine.tick],
            poll=lambda: self.poller.poll_all(),
            poll_seconds=settings.poll_seconds,
        )

    def serves(self, repo: str) -> bool:
        """Whether this instance works on ``repo``: a development instance with a
        sandbox repository serves only that one."""
        return not (
            self.settings.dev_mode
            and self.settings.sandbox_repo
            and repo != self.settings.sandbox_repo
        )

    def set_dev_mode(self, on: bool) -> None:
        """Force every repository action into shadow (or stop forcing it)."""
        self.settings.dev_mode = on
        self.pipeline.dev_mode = on
        self.info.dev_mode = on

    async def context_for(self, job: Job, event: Event) -> PlaybookContext:
        profile = await self.profiles.active(job.repo) if job.repo else None
        info = self.info
        if profile is not None:
            info = ServiceInfo(
                **{
                    **self.info.__dict__,
                    "pack_skill_dirs": self.packs.skill_dirs(profile.extends),
                },
            )
        return PlaybookContext(
            job=job,
            repo=job.repo,
            profile=profile,
            forge=self.a.forge,
            ci=CiReader(self.a.ci),
            agents=_JobRunner(self.a.agents, job.id),
            memory=MemoryReader(self.a.memory),
            knowledge=self.knowledge,
            profiles=self.profiles,
            store=self.a.store,
            workspace=self.a.workspace,
            sandbox=self.a.sandbox,
            labs=LabStatus(self.a.labs),
            clock=self.a.clock,
            service=info,
        )

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        await self.actuator.reconcile_intents()
        await self.questions.cancel_orphans()
        self.bus.start()
        self.engine.start()
        self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.engine.stop()
        await self.actuator.stop()
        await self.bus.stop()

    async def _question_channel(self, repo: str | None) -> str | None:
        """The notification channel for agents' questions about ``repo``."""
        if repo is None:
            return None
        profile = await self.profiles.active(repo)
        if profile is None:
            return None
        return profile.notify.get("agent_question") or profile.notify.get("default")

    async def memory_sync(self) -> None:
        if await self.a.memory.refresh():
            self.profiles.invalidate()
            for record in await self.store.list_repos():
                await self.bus.publish(
                    Event(
                        kind=EventKind.MEMORY_CHANGED,
                        repo=record.repo,
                        payload={"paths": ["remote"]},
                    )
                )
        await self.actuator.sweep_memory()

    # -- operations used by the dashboard and the CLI ----------------------------------

    async def add_repo(self, repo: str, added_by: str) -> RepoRecord:
        """Start onboarding. The repository stays inactive until its maintainers approve a profile.

        Raises:
            ValueError: This is a development instance and ``repo`` is not its sandbox.
        """
        if not self.serves(repo):
            raise ValueError(
                f"this development instance serves only its sandbox repository "
                f"{self.settings.sandbox_repo}, not {repo}"
            )
        record = await self.store.get_repo(repo)
        if record is None:
            record = RepoRecord(
                repo=repo, active=False, added_by=added_by, added_at=self.clock.now()
            )
        lock = await self.profiles.lock(repo)
        record.active = lock is not None
        await self.store.save_repo(record)
        await self.store.add_audit(
            AuditEntry(
                kind="config", repo=repo, actor=added_by, summary="repository added"
            )
        )
        if lock is None:
            await self.bus.publish(
                Event(kind=EventKind.REPO_ADDED, repo=repo, payload={"by": added_by})
            )
        return record

    async def approve_memory_change(
        self, change_id: str, login: str
    ) -> tuple[list[GateDecision], str | None]:
        """Record ``login``'s approval for each affected repository they maintain, then try to merge."""
        change = await self.store.get_memory_change(change_id)
        if change is None or change.state != MemoryChangeState.OPEN:
            raise ValueError(f"memory change {change_id} is not open")
        decisions = []
        for repo in change.repos:
            current = await self.profiles.active(repo)
            decision = await self.gate.check(repo, login, current)
            decisions.append(decision)
            if decision.allowed and login not in change.approvals.get(repo, []):
                change.approvals.setdefault(repo, []).append(login)
        await self.store.save_memory_change(change)
        await self.store.add_audit(
            AuditEntry(
                kind="memory",
                actor=login,
                summary=f"approval for {change.title}",
                data={
                    "change": change.id,
                    "decisions": [d.model_dump(mode="json") for d in decisions],
                },
            )
        )
        state = await self.actuator.merge_memory(change_id)
        return decisions, state

    async def reject_memory_change(
        self, change_id: str, login: str, reason: str
    ) -> MemoryChange:
        change = await self.store.get_memory_change(change_id)
        if change is None or change.state != MemoryChangeState.OPEN:
            raise ValueError(f"memory change {change_id} is not open")
        for repo in change.repos:
            decision = await self.gate.check(
                repo, login, await self.profiles.active(repo)
            )
            if not decision.allowed:
                raise PermissionError(decision.reason)
        change.state = MemoryChangeState.REJECTED
        change.rejected_reason = f"{login}: {reason}"
        await self.store.save_memory_change(change)
        await self.a.memory.drop_change(change)
        await self.store.add_audit(
            AuditEntry(
                kind="memory", actor=login, summary=f"rejected {change.title}: {reason}"
            )
        )
        return change


class _JobRunner:
    """Tags each agent task with the job that asked for it."""

    def __init__(self, inner: AgentRunner, job_id: str) -> None:
        self._inner = inner
        self._job = job_id

    async def run(self, task: Any, route: Route, output_model: Any = None) -> Any:
        return await self._inner.run(
            task.model_copy(update={"job_id": self._job}), route, output_model
        )

    async def health(self) -> Any:
        return await self._inner.health()
