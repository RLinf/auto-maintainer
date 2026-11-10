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
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .actuator.context import ExecContext, LabRunner
from .actuator.executor import Actuator
from .actuator.lab_sessions import LabSessions
from .actuator.pipeline import ActionPipeline
from .domain.actions import ActionKind, ActionStatus, MemoryChangePayload, make_action
from .domain.agents import AgentInstall, AgentLogin, AgentQuota
from .domain.clock import Clock
from .domain.events import Event, EventKind
from .domain.memory import (
    MemoryChange,
    MemoryChangeState,
    Tier,
    lock_path,
    profile_path,
    repo_dir,
)
from .domain.ports import (
    AgentInstaller,
    AgentRunner,
    BranchPublisher,
    CiProvider,
    ForgeReader,
    ForgeWriter,
    LabHost,
    MemoryStore,
    Notifier,
    PersonalAgents,
    Sandbox,
    SecretStore,
    ServiceUpdater,
    Store,
    UserTokens,
    Workspace,
)
from .domain.profile import Route
from .domain.records import AuditEntry, Job, RepoRecord
from .domain.secrets import find_secrets, redact_secrets
from .domain.settings import (
    LEGACY_RUNTIMES_PATH,
    SETTINGS_PATH,
    LabMachine,
    ProviderBinding,
    RuntimeConfig,
    ServiceConfig,
    SettingsError,
    SettingsStatus,
    UserToken,
    check_settings,
    effective_agents,
    effective_routing,
    parse_legacy_runtimes,
    parse_settings,
)
from .engine.bus import EventBus
from .engine.engine import Engine
from .engine.poller import Poller
from .engine.progress import JobProgress
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
from .profiles.governance import GovernanceRecords
from .profiles.knowledge import Knowledge
from .profiles.layers import ProfileError, content_hash
from .profiles.loader import ProfileLoader
from .profiles.lock import parse_lock
from .profiles.packs import PackLibrary
from .profiles.service import ProfileService

log = logging.getLogger(__name__)

INSTALL_LINES = 12
"""How many of an installer's last lines of output the Settings page shows."""
SIGNED_IN_SECONDS = 60
"""How long whether an agent CLI is signed in is shown before it is asked again."""
QUOTA_SECONDS = 300
"""How long an agent's quota reading is shown before its CLI is asked again."""
QUOTA_FRESH_SECONDS = 30
"""The youngest reading a request for a fresh one still gets, so that asking again
and again does not run the CLI every time."""


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
    user_tokens: UserTokens | None = None
    """Signed-in maintainers' own GitHub tokens, for posting reviews as them."""
    writer_for_user: (
        Callable[[str, UserToken], Awaitable[ForgeWriter | None]] | None
    ) = None
    """A forge writer acting as a person with their token (refreshing it when it
    expired and can be refreshed), or ``None`` when the token no longer works."""
    publisher_for_user: (
        Callable[[str, UserToken], Awaitable[BranchPublisher | None]] | None
    ) = None
    """A branch publisher pushing as a person with their token, for fix PRs."""
    people: PersonalAgents | None = None
    """Each person's own agent accounts (``store/people.py``)."""
    installer: AgentInstaller | None = None
    """Installs agent CLIs on this host when the owner of the service asks."""
    background: list[Any] = field(default_factory=list)
    """Pieces that run alongside the service, each with ``start`` and ``stop`` (the
    agents' sandbox and its GitHub read proxy)."""
    agent_sandbox: Any = None
    """The agents' sandbox control (``describe``, ``configure``) for the Settings
    page; ``None`` where the composition root gives none."""


@dataclass
class ServiceSettings:
    login: str
    identities: set[str]
    routing: dict[str, Route]
    defaults_profile: dict[str, Any]
    skills_dir: Path
    scratch_dir: Path
    defaults_governance: dict[str, Any] = field(default_factory=dict)
    """The skeleton of a repository's governance record
    (``config/defaults/governance.yaml``)."""
    pack_dirs: list[Path] = field(default_factory=list)
    extra_packs: dict[str, str] = field(default_factory=dict)
    host_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    schedules: dict[str, str] = field(default_factory=dict)
    pools: dict[str, int] = field(
        default_factory=lambda: {"io": 8, "agent": 3, "lab": 1, "quality": 2}
    )
    dev_mode: bool = False
    write_caps: tuple[int, int] = (120, 40)
    code_repo: str | None = None
    poll_seconds: int = 120
    default_agent: str | None = None
    """The agent chosen during setup (``local.yaml``); the service settings override it."""
    operators: set[str] = field(default_factory=set)
    base_url: str | None = None
    """Where the dashboard is reached, for links in alerts."""
    code_branch: str = "main"
    owner: str | None = None
    """The one person who owns this deployment (``local.yaml``); see
    ``ConfigGate.check_service``."""
    confirm_deploy_after: float = 60.0
    """Seconds of running before a fresh deploy counts as healthy (no rollback)."""
    sandbox_repo: str | None = None
    """In dev mode, the only repository the service serves and may act on."""
    lab_socket: Path | None = None
    """Where reproductions' agents reach their sessions on test machines."""


class Service:
    def __init__(
        self,
        adapters: Adapters,
        settings: ServiceSettings,
        playbooks: list[Playbook] | None = None,
    ) -> None:
        self.a = adapters
        self._quotas: dict[str, AgentQuota] = {}
        """Each agent's last quota reading (``agent_quota``)."""
        self.logins: dict[str, Any] = {}
        """Agent sign-ins started from the dashboard, by id (``agents/login.py``)."""
        self._signed_in: dict[tuple[str, str | None], tuple[datetime, bool | None]] = {}
        self.installs: dict[str, AgentInstall] = {}
        """The latest install of each agent CLI asked for since the service started."""
        self._install_tasks: set[asyncio.Task[None]] = set()
        self.settings = settings
        self.store = adapters.store
        self.clock = adapters.clock
        self.knowledge = Knowledge(adapters.memory)
        self.governance = GovernanceRecords(
            adapters.memory, settings.defaults_governance
        )
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
        self.gate = ConfigGate(
            adapters.forge,
            settings.identities | {settings.login},
            maintainers=self.governance.maintainers_of,
        )
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
            "default_agent": settings.default_agent,
        }
        self.service_config = ServiceConfig()
        """The settings in force, from ``config/service.yaml``."""
        self.settings_status = SettingsStatus(applied=True)
        self.closing = asyncio.Event()
        """Set when shutdown begins; the dashboard's live streams end on it."""
        self.restart_requested = asyncio.Event()
        """Set after a deploy; ``am serve`` then exits for its supervisor to restart it."""
        self._confirming: asyncio.Task[None] | None = None
        self._polls: dict[str, asyncio.Task[None]] = {}
        """Polls started because a repository's approved profile changed, by repository."""
        self.questions = QuestionDesk(
            adapters.store,
            self.bus,
            adapters.notifier,
            adapters.clock,
            channel_for=self._question_channel,
            dashboard_url=settings.base_url,
        )
        adapters.agents.bind_questions(self.questions)
        self.machines: dict[str, LabMachine] = {}
        """Test machines from the service settings; playbooks read this same dict."""
        self.progress = JobProgress(adapters.store, self.bus, adapters.clock)
        """What agents do in each job, for the dashboard's live view."""
        adapters.agents.bind_progress(self.progress.observe)
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
            workspace=adapters.workspace,
            updater=adapters.updater,
            request_restart=self.request_restart,
            writer_as=self.user_writer,
            publisher_as=self.user_publisher,
            lab_progress=self.progress.lab,
        )
        if adapters.lab_runner is not None and hasattr(
            adapters.lab_runner, "progress_sink"
        ):
            # A session adopted after a restart still shows its output to its job.
            adapters.lab_runner.progress_sink = self.progress.lab
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
            machines=self.machines,
            lab_socket=settings.lab_socket,
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
        self.engine.on_change = lambda job: self.progress.changed(
            job.id, job.repo, job.number
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
            governance=self.governance,
            lab_sessions=LabSessions(self.a.lab_runner),
            workspace=self.a.workspace,
            sandbox=self.a.sandbox,
            labs=LabStatus(self.a.labs),
            clock=self.a.clock,
            service=info,
            own_reviews=await self.posted_as_people(job.repo)
            if job.repo
            else frozenset(),
            drafts=self.pipeline,
        )

    async def user_writer(self, login: str) -> ForgeWriter | None:
        """A forge writer acting as ``login`` with their own sign-in, if it still works."""
        if self.a.user_tokens is None or self.a.writer_for_user is None:
            return None
        token = await self.a.user_tokens.get(login)
        if token is None:
            return None
        return await self.a.writer_for_user(login, token)

    async def user_publisher(self, login: str) -> BranchPublisher | None:
        """A branch publisher pushing as ``login`` with their own sign-in, if it works."""
        if self.a.user_tokens is None or self.a.publisher_for_user is None:
            return None
        token = await self.a.user_tokens.get(login)
        if token is None:
            return None
        return await self.a.publisher_for_user(login, token)

    async def can_post_as(self, login: str) -> bool:
        """Whether ``login`` signed in in a way that lets the service post as them."""
        return (
            self.a.user_tokens is not None
            and self.a.writer_for_user is not None
            and await self.a.user_tokens.get(login) is not None
        )

    async def posted_as_people(self, repo: str) -> frozenset[str]:
        """Review actions of this service that a maintainer posted as themselves; their
        markers count as the service's reviews whoever's account posted them."""
        return frozenset(
            record.action.id
            for record in await self.store.list_actions(
                repo=repo,
                kinds=[ActionKind.REVIEW_POST],
                statuses=[ActionStatus.EXECUTED],
                limit=1000,
            )
            if record.post_as
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
        for piece in self.a.background:
            await piece.start()
        await self.reload_settings()
        await self.actuator.reconcile_intents()
        await self.questions.cancel_orphans()
        await self.engine.recover()
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
        for task in self._polls.values():
            task.cancel()
        await self.scheduler.stop()
        await self.engine.stop()
        await self.actuator.stop()
        await self.bus.stop()
        for piece in reversed(self.a.background):
            await piece.stop()

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
        """The service's repository, whose admin owns this deployment."""
        return self.settings.code_repo

    async def service_gate(self, login: str) -> GateDecision:
        """Whether ``login``, a person signed in to the dashboard, acts as this
        deployment's owner (live, on the forge): changes the service settings, the
        host setup, and the service's own code."""
        return await self.gate.check_service(
            self.settings.code_repo, self.settings.owner, login, person=True
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
        routes = effective_routing(
            self.default_routing, config, self.host_values["default_agent"]
        )
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
        self.machines.clear()
        self.machines.update(config.labs)
        configure = getattr(self.a.lab_runner, "configure", None)
        if configure is not None:
            configure(dict(config.labs))
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
                models=tuple(p.model_ids),
                api_key=key,
                specs=tuple(p.models),
            )
        return out

    async def propose_settings(
        self, text: str, login: str, title: str, *, approve: bool = True
    ) -> tuple[MemoryChange, GateDecision]:
        """Open a change to ``config/service.yaml``. With ``approve`` and a ``login`` who
        passes the service gate, making the change approves it and it applies at once;
        otherwise it waits on the Settings page for the owner of the service's
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
        if (
            event.kind == EventKind.ACTION_EXECUTED
            and event.repo
            and event.number is not None
            and self.serves(event.repo)
        ):
            # Show what the service just wrote (a label, a review) without waiting
            # for the next poll.
            try:
                await self.poller.poll_pr(event.repo, event.number)
            except Exception:
                log.exception("refreshing %s#%s failed", event.repo, event.number)
            return
        if event.kind != EventKind.MEMORY_CHANGED:
            return
        paths = set(event.payload.get("paths") or [])
        if paths & {SETTINGS_PATH, LEGACY_RUNTIMES_PATH, "remote"}:
            await self.reload_settings()
        if event.repo and lock_path(event.repo) in paths:
            self.poll_now(event.repo)

    def installable(self) -> dict[str, str]:
        """The agent CLIs this host can install from the dashboard, with their source."""
        return self.a.installer.sources() if self.a.installer is not None else {}

    def start_install(self, name: str, by: str) -> AgentInstall:
        """Install agent ``name``'s CLI in the background, unless it is being
        installed already. ``by`` passed ``service_gate`` (maintainers of the
        service's repository install software on its host)."""
        current = self.installs.get(name)
        if current is not None and current.state == "running":
            return current
        sources = self.installable()
        if self.a.installer is None or name not in sources:
            raise ValueError(f"{name} cannot be installed on this host")
        state = AgentInstall(
            name=name, by=by, source=sources[name], started_at=self.clock.now()
        )
        self.installs[name] = state
        task = asyncio.create_task(self._install(state), name=f"install-{name}")
        self._install_tasks.add(task)
        task.add_done_callback(self._install_tasks.discard)
        return state

    async def _install(self, state: AgentInstall) -> None:
        assert self.a.installer is not None

        async def on_line(line: str) -> None:
            text = redact_secrets(line.rstrip())[0]
            if text:
                state.lines = [*state.lines[-(INSTALL_LINES - 1) :], text[:300]]

        try:
            done = await self.a.installer.install(state.name, on_line)
        except Exception as exc:  # shown on the Settings page as it is
            state.state, state.error = "failed", redact_secrets(str(exc))[0]
            log.warning("installing %s failed: %s", state.name, exc)
        else:
            state.state, state.version = "done", done.version
        state.finished_at = self.clock.now()
        await self.store.add_audit(
            AuditEntry(
                kind="service",
                actor=state.by,
                summary=f"installed {state.name} {state.version} from {state.source}"
                if state.state == "done"
                else f"installing {state.name} failed: {state.error}",
            )
        )

    def runtime(self, name: str, account: str | None = None) -> Any:
        """Agent ``name``'s runtime, or a copy of it on ``account``'s own home folder
        (a person's login); ``None`` when there is no such agent."""
        runtime = (getattr(self.a.agents, "runtimes", None) or {}).get(name)
        if runtime is None or account is None:
            return runtime
        if self.a.people is None or not hasattr(runtime, "account_home"):
            return None
        mine = copy.copy(runtime)
        mine.account = account.lower()
        mine.account_home = self.a.people.home(account)
        mine.provider = None
        return mine

    def account_home(self, account: str | None) -> Path:
        """The home folder agent CLIs sign in to for ``account``: the service user's
        own for the service's account, else the person's own (``store/people.py``)."""
        if account is None:
            return Path.home()
        if self.a.people is None:
            raise ValueError("this host keeps no personal agent accounts")
        return self.a.people.home(account)

    def agents_isolated(self, name: str) -> bool:
        """Whether agent ``name``'s runs each get a container of their own, which a
        person's own account needs: on the host, every run could read every
        person's sign-in."""
        runtime = self.runtime(name)
        return getattr(runtime, "container_sandbox", None) is not None

    def start_login(
        self, name: str, account: str | None, by: str, option: str | None = None
    ) -> AgentLogin:
        """Start agent ``name``'s own sign-in for ``account`` (``None``: the service's,
        which ``by`` may change only past ``service_gate``; else ``by``'s own), to the
        account kind ``option`` where the CLI offers several, replacing one still
        waiting for the same account.

        Raises:
            ValueError: The agent cannot sign in here, or offers no ``option``.
        """
        runtime = self.runtime(name)
        can = getattr(runtime, "can_sign_in", None)
        if runtime is None or can is None or not can():
            raise ValueError(f"{name} has no sign-in of its own")
        for key, process in list(self.logins.items()):
            st = process.state
            if (st.agent, st.account) == (name, account) and st.state == "waiting":
                task = asyncio.create_task(process.cancel())
                self._install_tasks.add(task)
                task.add_done_callback(self._install_tasks.discard)
                del self.logins[key]
        state = AgentLogin(
            agent=name, account=account, by=by, started_at=self.clock.now()
        )
        try:
            self.logins[state.id] = runtime.start_login(  # type: ignore[attr-defined]
                self.account_home(account), state, option
            )
        except RuntimeError as e:
            raise ValueError(str(e)) from e
        self._signed_in.pop((name, account), None)
        return state

    def forget_signed_in(self, name: str, account: str | None) -> None:
        """Ask the CLI again next time, after a sign-in or sign-out."""
        self._signed_in.pop((name, account), None)

    async def signed_in(self, name: str, account: str | None = None) -> bool | None:
        """Whether agent ``name``'s CLI is signed in for ``account``; ``None`` when it
        cannot tell. Kept for ``SIGNED_IN_SECONDS``."""
        kept = self._signed_in.get((name, account))
        now = self.clock.now()
        signed_since = any(
            p.state.agent == name
            and p.state.account == account
            and p.state.state == "done"
            and kept is not None
            and p.state.started_at >= kept[0]
            for p in self.logins.values()
        )
        if (
            kept is not None
            and not signed_since
            and (now - kept[0]).total_seconds() < SIGNED_IN_SECONDS
        ):
            return kept[1]
        runtime = self.runtime(name)
        check = getattr(runtime, "signed_in", None)
        if check is None or not self.agent_ready(name):
            return None
        found = await check(self.account_home(account))
        self._signed_in[(name, account)] = (now, found)
        return found

    async def agent_quota(
        self, name: str, fresh: bool = False, account: str | None = None
    ) -> AgentQuota | None:
        """What agent ``name``'s CLI says is left of ``account``'s limits (the
        service's own by default); ``None`` when it cannot report them. A reading is
        kept for ``QUOTA_SECONDS``, or ``QUOTA_FRESH_SECONDS`` when ``fresh``."""
        read = getattr(self.runtime(name, account), "quota", None)
        if read is None or not self.agent_ready(name):
            return None
        key = name if account is None else f"{name}@{account.lower()}"
        kept = self._quotas.get(key)
        now = self.clock.now()
        age = (now - kept.read_at).total_seconds() if kept else None
        if age is not None and age < (QUOTA_FRESH_SECONDS if fresh else QUOTA_SECONDS):
            return kept
        found = await read()
        if found is not None:
            self._quotas[key] = found.model_copy(update={"read_at": now})
        return self._quotas.get(key)

    def agent_ready(self, name: str) -> bool:
        """Whether agent ``name`` is enabled and installed on this host; ``True`` for
        runners that cannot tell (the test fakes)."""
        ready = getattr(self.a.agents, "ready", None)
        return bool(ready(name)) if callable(ready) else True

    def runs_on(self, route: Route | None) -> str | None:
        """The agent ``route`` runs on now: its first agent that is ready, else its
        primary."""
        if route is None:
            return None
        runs_on = getattr(self.a.agents, "runs_on", None)
        return (runs_on(route) if callable(runs_on) else None) or route.primary

    def poll_now(self, repo: str) -> bool:
        """Read ``repo``'s PRs now instead of at the next scheduled poll, in the
        background (a first poll reads every open PR): after its approved profile
        changed, which a first approval makes active, or when a maintainer asks.

        Returns whether a poll started; ``False`` when one is already running.
        """
        running = self._polls.get(repo)
        if not self.serves(repo) or (running is not None and not running.done()):
            return False

        async def poll() -> None:
            record = await self.store.get_repo(repo)
            if record is None or not record.active:
                return
            try:
                await self.poller.poll_repo(repo)
            except Exception:
                log.exception("polling %s after its profile changed failed", repo)

        self._polls[repo] = asyncio.create_task(poll(), name=f"poll {repo}")
        return True

    # -- operations used by the dashboard and the CLI ----------------------------------

    async def add_repo(self, repo: str, added_by: str) -> RepoRecord:
        """Start onboarding. The repository stays inactive until its maintainers approve a profile.

        Adding it again drafts its profile again while the memory repo has no profile
        layer for it, as when the onboarding agent never ran; the draft is a change
        its maintainers approve like any other.

        Raises:
            ValueError: This is a development instance and ``repo`` is not its sandbox.
        """
        if not self.serves(repo):
            raise ValueError(
                f"this development instance serves only its sandbox repository "
                f"{self.settings.sandbox_repo}, not {repo}"
            )
        repo = await self.canonical_repo(repo)
        for known in await self.store.list_repos():
            if known.repo != repo and known.repo.lower() == repo.lower():
                raise ValueError(
                    f"{repo} is already added as {known.repo}; rename it with "
                    f"`am repo rename {known.repo} {repo}`"
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
        drafted = await self.a.memory.read_text(profile_path(repo)) is not None
        if lock is None or not drafted:
            await self.bus.publish(
                Event(kind=EventKind.REPO_ADDED, repo=repo, payload={"by": added_by})
            )
        return record

    async def rename_repo(self, old: str, new: str, login: str) -> int:
        """Rename repository ``old`` to ``new``, its spelling on GitHub, when they
        differ only in letter case: its memory folder (the approved lock is stamped
        with the new name, nothing else in it changes), and every record in the store.
        Only someone who may configure the repository may do it, as for any change to
        its profile. Run it while the service is stopped. Returns the rows changed.

        Raises:
            ValueError: The names differ in more than case, ``new`` is not GitHub's
                spelling, ``old`` is not a repository the service serves, or
                ``login`` may not configure it.
        """
        if old == new or old.lower() != new.lower():
            raise ValueError("a rename may change only the letter case of a name")
        if await self.store.get_repo(old) is None:
            raise ValueError(f"the service has no repository named {old}")
        canonical = await self.canonical_repo(old)
        if canonical != new:
            raise ValueError(f"GitHub spells it {canonical}, not {new}")
        decision = await self.gate.check(
            new, login, await self.profiles.active(old), person=True
        )
        if not decision.allowed:
            raise ValueError(decision.reason)
        files: dict[str, str | None] = {}
        moved: dict[str, str | None] = {}
        for path in await self.a.memory.list_files(repo_dir(old)):
            text = await self.a.memory.read_text(path)
            files[path] = (
                None  # deletions first: on a case-insensitive disk the paths collide
            )
            target = repo_dir(new) + path[len(repo_dir(old)) :]
            if path == lock_path(old) and text:
                lock = parse_lock(text, path)
                if lock is not None:
                    profile = {**lock.profile, "repo": new}
                    text = lock.model_copy(
                        update={
                            "repo": new,
                            "profile": profile,
                            "hash": content_hash(profile),
                        }
                    ).to_yaml()
            moved[target] = text
        if files:
            await self.a.memory.commit_direct(
                {**files, **moved},
                f"memory: rename {old} to {new}, its name on GitHub",
                login,
            )
        changed = await self.store.rename_repo(old, new)
        self.profiles.invalidate(old)
        self.profiles.invalidate(new)
        await self.store.add_audit(
            AuditEntry(
                kind="config",
                repo=new,
                actor=login,
                summary=f"renamed {old} to {new} ({changed} records, {len(files)} memory files)",
            )
        )
        return changed

    async def canonical_repo(self, repo: str) -> str:
        """GitHub's spelling of ``repo`` (names are not case-sensitive there), or
        ``repo`` itself when GitHub cannot say."""
        try:
            return (await self.a.forge.get_repo(repo)).name or repo
        except Exception as e:  # unreachable or unknown: keep what was given
            log.warning("could not read %s's name from GitHub: %s", repo, e)
            return repo

    async def open_lock_change(
        self, repo: str, profile_hash: str
    ) -> MemoryChange | None:
        """The open change that locks ``repo`` at ``profile_hash``, if one waits.

        Approving the same candidate again (after its write to the memory repo
        failed) retries that change instead of staging a second one, which would
        conflict with the first when both merge.
        """
        path = lock_path(repo)
        for change in await self.store.list_memory_changes(MemoryChangeState.OPEN):
            if change.service or change.repos != [repo] or path not in change.files:
                continue
            try:
                lock = parse_lock(change.files[path], path)
            except ProfileError:
                continue
            if lock is not None and lock.hash == profile_hash:
                return change
        return None

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
            decision = await self.gate.check(repo, login, current, person=True)
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
                repo, login, await self.profiles.active(repo), person=True
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
