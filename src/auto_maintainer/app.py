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
from urllib.parse import urlparse

import yaml

from .agents.container import ContainerSandbox, SandboxStarter, host_tools, image_tag
from .agents.github_proxy import GitHubReadProxy
from .agents.registry import budgets_of, build_runtimes, merge_configs
from .agents.router import AgentRouter
from .config import CONFIG_DIR, HostConfig, load_host_config, save_host_config
from .domain.clock import SystemClock
from .domain.profile import Route
from .domain.records import AgentRunRecord
from .domain.settings import ProviderBinding, RuntimeConfig, UserToken
from .github import GitHubActionsCi, GitHubClient, GitHubReader, GitHubWriter
from .github.auth import StaticTokenProvider
from .labs.executor import GpuThresholds, LabExecutor
from .labs.hosts import LocalLabHost, SshLabHost
from .memory.git_store import GitMemoryStore
from .notify.webhook import ChannelConfig, WebhookNotifier
from .resources import data_dir, defaults_dir
from .selfupdate import GitServiceUpdater
from .service import Adapters, Service, ServiceSettings
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

    # The service acts as the GitHub CLI login made on the setup page, or, without
    # one, the account the host user's own GitHub CLI is logged in to.
    tokens = StaticTokenProvider(None, gh_config_dir=cfg.github.gh_config_dir)
    client = GitHubClient(tokens, api_url=cfg.github.api_url)
    reader = GitHubReader(client)
    fresh_reader = GitHubReader(client, snapshot_cache_seconds=0)
    writer = GitHubWriter(client)
    ci = GitHubActionsCi(client)
    login = await reader.service_login()

    store = await SqliteStore.open(data / "am.db")

    remote = cfg.memory.remote
    memory = GitMemoryStore(
        cfg.memory_path,
        remote_url=f"{cfg.github.web_url}/{remote}.git" if remote else None,
        remote_repo=remote,
        branch=cfg.memory.branch,
        token=(lambda: tokens.token(remote)) if remote else None,
    )
    await memory.open()

    # Operator settings from the memory repo (design 8.1): what the service runs on.
    op = cfg.memory_path / "config"
    runtimes_cfg = merge_configs((_yaml(op / "runtimes.yaml") or {}).get("runtimes"))
    runtimes = build_runtimes(runtimes_cfg, runs_dir=data / "runs")

    proxy = GitHubReadProxy(
        data / "sandbox" / "github.sock",
        lambda: tokens.token(None),
        api_host=urlparse(cfg.github.api_url).hostname or "api.github.com",
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
            github_socket=proxy.socket_path,
            tools=host_tools(),
            mounts=[Path(os.path.expanduser(m)) for m in cfg.sandbox.mounts],
        ),
        lambda: router.runtimes,
        enabled=cfg.sandbox.enabled,
        save=save_sandbox,
    )
    background: list[Any] = [proxy, starter]

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

    router = AgentRouter(
        runtimes,
        clock=clock,
        budgets=budgets_of(runtimes_cfg),
        usage_today=usage_today,
        record_run=record_run,
    )

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
        return GitHubWriter(
            GitHubClient(
                StaticTokenProvider(token=token.token), api_url=cfg.github.api_url
            )
        )

    async def publisher_for_user(login: str, token: UserToken) -> Any:
        """Pushes branches as ``login``; ``None`` when their token no longer works."""
        if await writer_for_user(login, token) is None:
            return None
        fresh = await user_tokens.get(login) or token

        async def user_token(repo: str) -> str | None:
            return fresh.token

        return GitWorkspace(data, token=user_token, forge_url=cfg.github.web_url)

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

    workspace = GitWorkspace(data, token=tokens.token, forge_url=cfg.github.web_url)
    checkout = (
        Path(cfg.self_update.checkout).expanduser()
        if cfg.self_update.checkout
        else Path(__file__).resolve().parents[2]
    )
    updater = (
        GitServiceUpdater(
            checkout,
            data / "deploys",
            branch=cfg.self_update.branch,
            remote=cfg.self_update.remote,
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
        sandbox=LocalSandbox(),
        notifier=WebhookNotifier(channels),
        clock=clock,
        labs=labs,
        lab_runner=lab_runner,
        publisher=workspace if cfg.code_repo else None,
        updater=updater,
        secrets=FileSecretStore(CONFIG_DIR / "provider-keys.json"),
        reconfigure=reconfigure,
        user_tokens=user_tokens,
        writer_for_user=writer_for_user,
        publisher_for_user=publisher_for_user,
        background=background,
        agent_sandbox=starter,
    )
    settings = ServiceSettings(
        login=login,
        identities={login},
        routing=routing,
        defaults_profile=defaults,
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
        code_branch=cfg.self_update.branch,
        code_approver=cfg.self_update.approve_permission,
    )
    return Service(adapters, settings)
