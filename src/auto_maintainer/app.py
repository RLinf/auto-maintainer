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

"""The composition root: the only module that imports adapters (design 4.4)."""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .agents.container import (
    ContainedSandbox,
    ContainerSandbox,
    SandboxStarter,
    host_tools,
    image_tag,
)
from .agents.install import AgentInstaller, installs_dir
from .agents.registry import budgets_of, build_runtimes, merge_configs
from .agents.router import AgentRouter
from .config import CONFIG_DIR, HostConfig, load_host_config, save_host_config
from .domain import forges
from .domain.clock import SystemClock
from .domain.profile import Route
from .domain.records import AgentRunRecord
from .domain.settings import ProviderBinding, RuntimeConfig, UserToken
from .forges.base import ForgeSite
from .forges.registry import kind as forge_kind
from .forges.router import ForgeRouter
from .labs.executor import GpuThresholds, LabExecutor
from .labs.hosts import LocalLabHost, SshLabHost
from .labs.sessions import LabSessionServer
from .memory.git_store import GitMemoryStore
from .notify.webhook import ChannelConfig, WebhookNotifier
from .resources import data_dir, defaults_dir
from .selfupdate import GitServiceUpdater, export_source
from .service import Adapters, Service, ServiceSettings
from .store.people import PeopleAgents
from .store.secrets import FileSecretStore, FileUserTokens
from .store.sqlite import SqliteStore
from .workspace.git import GitWorkspace, LocalSandbox

log = logging.getLogger(__name__)


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


async def build_service(cfg: HostConfig, dev: bool = False) -> Service:
    clock = SystemClock()
    data = cfg.data_path
    data.mkdir(parents=True, exist_ok=True)
    # Every forge the service serves (design 5.16), each built by its kind; the
    # routers hand each call to the forge a repository is on.
    sites = [
        ForgeSite(
            id=f.id,
            kind=f.kind,
            web_url=f.web,
            api_url=f.api,
            cli_config_dir=f.cli_config_dir,
            data_dir=data,
        )
        for f in cfg.forges
    ]
    forges.configure(
        cfg.default_forge.id, {s.id: forge_kind(s.kind).describe(s) for s in sites}
    )
    endpoints = {s.id: await forge_kind(s.kind).build(s) for s in sites}
    home = endpoints[cfg.default_forge.id]
    reader = ForgeRouter({k: e.reader for k, e in endpoints.items()})
    fresh_reader = ForgeRouter({k: e.fresh_reader for k, e in endpoints.items()})
    writer = ForgeRouter({k: e.writer for k, e in endpoints.items()})
    ci = ForgeRouter({k: e.ci for k, e in endpoints.items()})
    login = home.service_login

    async def service_token(repo: str | None) -> str | None:
        """The service's token on ``repo``'s forge (the default forge's for none)."""
        at = endpoints[forges.forge_of(repo)] if repo else home
        return await at.token(repo)

    def grant_scopes(repo: str | None, scopes: list[str]) -> Any:
        """A request adding ``scopes`` to the service's sign-in on ``repo``'s forge."""
        at = endpoints[forges.forge_of(repo)] if repo else home
        if at.grant_scopes is None:
            raise RuntimeError(f"{at.site.id} cannot add scopes to its sign-in here")
        return at.grant_scopes(scopes)

    def forget_token(repo: str | None) -> None:
        at = endpoints[forges.forge_of(repo)] if repo else home
        if at.forget_token is not None:
            at.forget_token()

    def remote_of(repo: str) -> tuple[str, str]:
        ref = forges.parse_repo(repo)
        at = endpoints[ref.forge]
        return f"{at.site.web_url}/{ref.path}.git", at.git_user

    store = await SqliteStore.open(data / "am.db")

    remote = cfg.memory.remote
    memory = GitMemoryStore(
        cfg.memory_path,
        remote_url=f"{home.site.web_url}/{remote}.git" if remote else None,
        remote_repo=remote,
        branch=cfg.memory.branch,
        token=(lambda: home.token(remote)) if remote else None,
        git_user=home.git_user,
    )
    await memory.open()

    # Operator settings from the memory repo (design 8.1): what the service runs on.
    op = cfg.memory_path / "config"
    runtimes_cfg = merge_configs((_yaml(op / "runtimes.yaml") or {}).get("runtimes"))
    runtimes = build_runtimes(runtimes_cfg, runs_dir=data / "runs")

    proxies = [e.read_proxy for e in endpoints.values() if e.read_proxy is not None]
    github_proxy = next(
        (e.read_proxy for e in endpoints.values() if e.site.kind == "github"), None
    )

    def save_sandbox(values: dict[str, Any]) -> None:
        """The Settings page's sandbox settings, into this host's ``local.yaml``."""
        host = load_host_config()
        host.sandbox = host.sandbox.model_copy(update=values)
        save_host_config(host)

    starter = SandboxStarter(
        ContainerSandbox(
            cfg.sandbox.image or image_tag(),
            cpus=cfg.sandbox.cpus,
            memory=cfg.sandbox.memory,
            pids=cfg.sandbox.pids,
            network=cfg.sandbox.network,
            github_socket=github_proxy.socket_path if github_proxy else None,
            tools=host_tools(),
            mounts=[Path(os.path.expanduser(m)) for m in cfg.sandbox.mounts],
            installs=installs_dir(data),
            # Every checkout an agent works in is a worktree of a mirror here
            # (``GitWorkspace.mirror``).
            git_roots=[data / "mirrors"],
        ),
        lambda: router.runtimes,
        enabled=cfg.sandbox.enabled,
        save=save_sandbox,
    )
    background: list[Any] = [*proxies, starter]

    async def usage_today(runtime: str) -> tuple[int, float]:
        now = clock.now()
        start = now - timedelta(
            hours=now.hour,
            minutes=now.minute,
            seconds=now.second,
            microseconds=now.microsecond,
        )
        return await store.agent_usage_since(runtime, start)

    async def record_run(run: AgentRunRecord) -> None:
        await store.add_agent_run(run)

    people = PeopleAgents(data / "people")
    checkout = (
        Path(cfg.self_update.checkout).expanduser()
        if cfg.self_update.checkout
        else Path(__file__).resolve().parents[2]
    )
    router = AgentRouter(
        runtimes,
        clock=clock,
        budgets=budgets_of(runtimes_cfg),
        usage_today=usage_today,
        record_run=record_run,
        service_code=await export_source(checkout, data / "service-code"),
    )
    router.people = people

    async def reconfigure(
        agents: dict[str, RuntimeConfig], providers: dict[str, ProviderBinding]
    ) -> None:
        """New service settings: rebuild the runtimes and hand them to the router."""
        built = build_runtimes(agents, runs_dir=data / "runs", providers=providers)
        starter.attach(built)
        router.reconfigure(built, budgets_of(agents))

    user_tokens = FileUserTokens(data / "user-tokens.json")

    async def writer_for_user(login: str, token: UserToken) -> Any:
        """A writer acting as ``login``; ``None`` once their token has expired (a
        GitHub CLI token does not; one kept from an older sign-in may), which also
        forgets it, so they sign in again."""
        if token.expires_at and token.expires_at <= clock.now():
            await user_tokens.delete(login)
            return None
        person = forges.parse_person(login)
        return endpoints[person.forge].writer_for_token(token.token, person.login)

    async def publisher_for_user(login: str, token: UserToken) -> Any:
        """Pushes branches as ``login``; ``None`` when their token no longer works."""
        if await writer_for_user(login, token) is None:
            return None
        fresh = await user_tokens.get(login) or token

        async def user_token(repo: str) -> str | None:
            return fresh.token

        return GitWorkspace(data, token=user_token, remote=remote_of)

    labs: dict[str, Any] = {}
    for name, lab in cfg.labs.items():
        labs[name] = (
            SshLabHost(name, lab.host or name)
            if lab.transport == "ssh"
            else LocalLabHost(name)
        )
    lab_runner = LabExecutor(
        labs,
        {n: GpuThresholds(**lab.gpu.model_dump()) for n, lab in cfg.labs.items()},
        store,
        clock,
    )
    lab_sessions = LabSessionServer(data / "sandbox" / "lab.sock", lab_runner)
    background.append(lab_sessions)

    channels = {
        k: ChannelConfig.model_validate(v)
        for k, v in ((_yaml(op / "channels.yaml") or {}).get("channels") or {}).items()
    }
    defaults = _yaml(defaults_dir() / "profile.yaml") or {}
    routing = {
        k: Route.model_validate(v)
        for k, v in (_yaml(defaults_dir() / "routing.yaml") or {}).items()
    }
    schedules = {
        **(_yaml(defaults_dir() / "schedules.yaml") or {}),
        **(_yaml(op / "schedules.yaml") or {}),
    }
    extra_packs = {}
    for pack in sorted((cfg.memory_path / "packs").glob("*/pack.yaml")):
        extra_packs[pack.parent.name] = pack.read_text(encoding="utf-8")

    workspace = GitWorkspace(data, token=service_token, remote=remote_of)

    async def code_source() -> tuple[str, str, str | None]:
        """The service's repository, fetched as the service's account there."""
        assert cfg.code_repo
        url, user = remote_of(cfg.code_repo)
        return url, user, await service_token(cfg.code_repo)

    updater = (
        GitServiceUpdater(
            checkout,
            data / "deploys",
            branch=cfg.self_update.branch,
            source=code_source,
            repo=cfg.code_repo or "origin",
            test_command=cfg.self_update.test_command,
        )
        if cfg.code_repo and (checkout / ".git").exists()
        else None
    )

    adapters = Adapters(
        forge=reader,
        actuator_reader=fresh_reader,
        writer=writer,
        ci=ci,
        memory=memory,
        store=store,
        agents=router,
        workspace=workspace,
        sandbox=ContainedSandbox(starter, LocalSandbox(), data / "sandbox"),
        notifier=WebhookNotifier(channels),
        clock=clock,
        labs=labs,
        lab_runner=lab_runner,
        publisher=workspace if cfg.code_repo else None,
        updater=updater,
        secrets=FileSecretStore(CONFIG_DIR / "provider-keys.json"),
        reconfigure=reconfigure,
        user_tokens=user_tokens,
        grant_scopes=grant_scopes,
        forget_token=forget_token,
        installer=AgentInstaller(data),
        people=people,
        writer_for_user=writer_for_user,
        publisher_for_user=publisher_for_user,
        background=background,
        agent_sandbox=starter,
    )
    settings = ServiceSettings(
        login=login,
        identities={forges.person_on(k, e.service_login) for k, e in endpoints.items()},
        routing=routing,
        defaults_profile=defaults,
        lab_socket=lab_sessions.socket_path,
        defaults_governance=_yaml(defaults_dir() / "governance.yaml") or {},
        skills_dir=data_dir("skills"),
        scratch_dir=data / "scratch",
        pack_dirs=[data_dir("packs")],
        extra_packs=extra_packs,
        host_overrides=cfg.host_overrides,
        schedules=schedules,
        pools=cfg.pools,
        dev_mode=dev,
        write_caps=cfg.write_caps,
        code_repo=cfg.code_repo,
        poll_seconds=cfg.poll_seconds,
        default_agent=cfg.default_agent,
        operators=set(cfg.operators),
        base_url=cfg.base_url,
        sandbox_repo=cfg.sandbox_repo,
        standby=cfg.standby,
        code_branch=cfg.self_update.branch,
        owner=cfg.owner,
    )
    return Service(adapters, settings)
