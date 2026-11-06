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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actuator.context import ExecContext, LabRunner
from .actuator.executor import Actuator
from .actuator.pipeline import ActionPipeline
from .domain.actions import ActionKind, MemoryChangePayload, make_action
from .domain.clock import Clock
from .domain.events import Event, EventKind
from .domain.memory import MemoryChange, MemoryChangeState, Tier
from .domain.permissions import Permission
from .domain.ports import (
    AgentRunner,
    BranchPublisher,
    CiProvider,
    ForgeReader,
    ForgeWriter,
    LabHost,
    MemoryStore,
    Notifier,
    Sandbox,
    SecretStore,
    ServiceUpdater,
    Store,
    Workspace,
)
from .domain.profile import Route
from .domain.records import AuditEntry, Job, RepoRecord
from .domain.secrets import find_secrets
from .domain.settings import (
    LEGACY_RUNTIMES_PATH,
    SETTINGS_PATH,
    ProviderBinding,
    RuntimeConfig,
    ServiceConfig,
    SettingsError,
    SettingsStatus,
    check_settings,
    effective_agents,
    effective_routing,
    parse_legacy_runtimes,
    parse_settings,
)
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
    publisher: BranchPublisher | None = None
    """Pushes self-improvement branches to the code repo (actuator only)."""
    updater: ServiceUpdater | None = None
    """Moves the service's own checkout to a new commit."""
    secrets: SecretStore | None = None
    """Values of the secrets the service settings name (providers' API keys)."""
    reconfigure: (
        Callable[
            [dict[str, RuntimeConfig], dict[str, ProviderBinding]], Awaitable[None]
        ]
        | None
    ) = None
    """Rebuilds the agent runtimes for new settings; supplied by the composition root."""


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
    code_branch: str = "main"
    code_approver: str = "maintain"
    """Permission on ``code_repo`` needed to merge the service's own PRs and deploy them."""
    confirm_deploy_after: float = 60.0
    """Seconds of running before a fresh deploy counts as healthy (no rollback)."""
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
        self.default_routing = {
            k: v.model_copy(deep=True) for k, v in settings.routing.items()
        }
        """The code's default routes (``config/defaults/routing.yaml``), before settings."""
        self.host_values = {
            "poll_seconds": settings.poll_seconds,
            "write_caps": settings.write_caps,
        }
        self.service_config = ServiceConfig()
        """The settings in force, from ``config/service.yaml``."""
        self.settings_status = SettingsStatus(applied=True)
        self.closing = asyncio.Event()
        """Set when shutdown begins; the dashboard's live streams end on it."""
        self.restart_requested = asyncio.Event()
        """Set after a deploy; ``am serve`` then exits for its supervisor to restart it."""
        self._confirming: asyncio.Task[None] | None = None
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
            code_branch=settings.code_branch,
            publisher=adapters.publisher,
            updater=adapters.updater,
            request_restart=self.request_restart,
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
            code_repo=settings.code_repo,
            code_branch=settings.code_branch,
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
        self.bus.subscribe(self._settings_event)
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

    def request_restart(self) -> None:
        self.restart_requested.set()

    async def _confirm_deploy(self) -> None:
        """After the service has run a while, mark a fresh deploy healthy and audit it."""
        if self.a.updater is None:
            return
        await asyncio.sleep(self.settings.confirm_deploy_after)
        record = await self.a.updater.confirm()
        if not record:
            return
        rolled_back = bool(record.get("rolled_back"))
        await self.store.add_audit(
            AuditEntry(
                kind="deploy",
                repo=self.settings.code_repo,
                summary=(
                    f"rolled back from {str(record.get('sha'))[:12]} to {str(record.get('previous_sha'))[:12]}"
                    if rolled_back
                    else f"now running {str(record.get('sha'))[:12]} (was {str(record.get('previous_sha'))[:12]})"
                ),
                data=record,
            )
        )

    async def start(self) -> None:
        await self.reload_settings()
        await self.actuator.reconcile_intents()
        await self.questions.cancel_orphans()
        self._confirming = asyncio.create_task(
            self._confirm_deploy(), name="confirm-deploy"
        )
        self.bus.start()
        self.engine.start()
        self.scheduler.start()

    async def stop(self) -> None:
        self.closing.set()
        if self._confirming is not None:
            self._confirming.cancel()
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

    # -- service settings (design 8.1) ------------------------------------------------

    def settings_repo(self) -> str | None:
        """The repository whose maintainers own the service's settings: the service's
        code repository, or else its memory repository."""
        return self.settings.code_repo or self.a.memory.remote_repo()

    async def service_gate(self, login: str) -> GateDecision:
        """Whether ``login`` may change the service's settings (live, on the forge)."""
        return await self.gate.check_service(
            self.settings_repo(), login, Permission(self.settings.code_approver)
        )

    async def settings_text(self) -> tuple[str, dict[str, Any]]:
        """The settings document as stored, and the older ``runtimes.yaml`` agents."""
        text = await self.a.memory.read_text(SETTINGS_PATH) or ""
        legacy = parse_legacy_runtimes(
            await self.a.memory.read_text(LEGACY_RUNTIMES_PATH)
        )
        return text, legacy

    async def check_settings_text(self, text: str) -> ServiceConfig:
        """Parse and check a settings document without applying it.

        Raises:
            SettingsError: It does not parse, or its pieces do not fit together.
        """
        if found := find_secrets(text):
            raise SettingsError(
                [
                    f"the document contains what looks like a credential ({', '.join(found)}); "
                    "name a secret instead and set its value in the key field"
                ]
            )
        config = parse_settings(text)
        _, legacy = await self.settings_text()
        problems = check_settings(config, self.default_routing, legacy)
        if problems:
            raise SettingsError(problems)
        return config

    async def reload_settings(self) -> SettingsStatus:
        """Apply ``config/service.yaml`` from the memory repo. A document that does not
        check keeps the previous settings in force and says why in ``settings_status``."""
        text, legacy = await self.settings_text()
        try:
            config = parse_settings(text)
            problems = check_settings(config, self.default_routing, legacy)
            if problems:
                raise SettingsError(problems)
            await self._apply_settings(config, legacy)
        except SettingsError as e:
            log.warning("service settings not applied: %s", e)
            self.settings_status = SettingsStatus(
                applied=False, problems=e.problems, loaded_at=self.clock.now()
            )
            return self.settings_status
        self.service_config = config
        self.settings_status = SettingsStatus(applied=True, loaded_at=self.clock.now())
        return self.settings_status

    async def _apply_settings(
        self, config: ServiceConfig, legacy: dict[str, Any]
    ) -> None:
        routes = effective_routing(self.default_routing, config)
        self.settings.routing.clear()
        self.settings.routing.update(routes)
        system = config.system
        poll = system.poll_seconds or self.host_values["poll_seconds"]
        caps = self.host_values["write_caps"]
        caps = (
            system.max_public_writes_per_hour
            if system.max_public_writes_per_hour is not None
            else caps[0],
            system.max_repo_public_writes_per_hour
            if system.max_repo_public_writes_per_hour is not None
            else caps[1],
        )
        self.settings.poll_seconds = poll
        self.scheduler.poll_seconds = poll
        self.settings.write_caps = caps
        self.pipeline.write_caps = caps
        if self.a.reconfigure is not None:
            await self.a.reconfigure(
                effective_agents(config, legacy), await self.provider_bindings(config)
            )

    async def provider_bindings(
        self, config: ServiceConfig
    ) -> dict[str, ProviderBinding]:
        """Each provider with its key from the secret store, for building runtimes."""
        out = {}
        for pid, p in config.providers.items():
            key = (
                await self.a.secrets.get(p.api_key)
                if p.api_key and self.a.secrets is not None
                else None
            )
            out[pid] = ProviderBinding(
                id=pid,
                name=p.name or pid,
                protocol=p.protocol,
                base_url=p.base_url,
                models=tuple(p.models),
                api_key=key,
            )
        return out

    async def propose_settings(
        self, text: str, login: str, title: str, *, approve: bool = True
    ) -> tuple[MemoryChange, GateDecision]:
        """Open a change to ``config/service.yaml``. With ``approve`` and a ``login`` who
        passes the service gate, making the change approves it and it applies at once;
        otherwise it waits on the Settings page for a maintainer of the service's
        repository.

        Raises:
            SettingsError: The document does not check.
        """
        await self.check_settings_text(text)
        current, _ = await self.settings_text()
        if current.strip() == text.strip():
            raise SettingsError(
                ["the settings already say this; there is nothing to change"]
            )
        decision = await self.service_gate(login)
        change = MemoryChange(
            tier=Tier.T2,
            title=title or "settings: update",
            description=f"Service settings change by {login}.",
            files={SETTINGS_PATH: text if text.endswith("\n") else text + "\n"},
            service=True,
            author=login,
            requested_by=login,
            from_own_signals=False,
            created_at=self.clock.now(),
        )
        if approve and decision.allowed:
            change.approvals = {decision.repo: [login]}
        await self.store.save_memory_change(change)
        await self.store.add_audit(
            AuditEntry(
                kind="config",
                actor=login,
                summary=f"service settings change: {change.title}",
                data={"change": change.id, "approved": bool(change.approvals)},
            )
        )
        await self.submit_memory_change(change)
        stored = await self.store.get_memory_change(change.id)
        if stored is not None and stored.state == MemoryChangeState.MERGED:
            await self.reload_settings()
        return stored or change, decision

    async def submit_memory_change(self, change: MemoryChange) -> None:
        """Hand a memory change to the memory gate through the actuator."""
        await self.pipeline.submit(
            make_action(
                ActionKind.MEMORY_CHANGE,
                self.settings_repo() or "service",
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            )
        )
        await self.actuator.drain()

    async def _settings_event(self, event: Event) -> None:
        if event.kind != EventKind.MEMORY_CHANGED:
            return
        paths = set(event.payload.get("paths") or [])
        if paths & {SETTINGS_PATH, LEGACY_RUNTIMES_PATH, "remote"}:
            await self.reload_settings()

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
        if change.service:
            decision = await self.service_gate(login)
            decisions.append(decision)
            if decision.allowed and login not in change.approvals.get(
                decision.repo, []
            ):
                change.approvals.setdefault(decision.repo, []).append(login)
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
        if change.service and state == MemoryChangeState.MERGED:
            await self.reload_settings()
        return decisions, state

    async def reject_memory_change(
        self, change_id: str, login: str, reason: str
    ) -> MemoryChange:
        change = await self.store.get_memory_change(change_id)
        if change is None or change.state != MemoryChangeState.OPEN:
            raise ValueError(f"memory change {change_id} is not open")
        if change.service and login != change.requested_by:
            decision = await self.service_gate(login)
            if not decision.allowed:
                raise PermissionError(decision.reason)
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
