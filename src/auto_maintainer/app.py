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
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .agents.registry import budgets_of, build_runtimes, merge_configs
from .agents.router import AgentRouter
from .config import HostConfig
from .domain.clock import SystemClock
from .domain.profile import Route
from .domain.records import AgentRunRecord
from .github import GitHubActionsCi, GitHubClient, GitHubReader, GitHubWriter
from .github.auth import GitHubAppTokenProvider, StaticTokenProvider
from .labs.executor import GpuThresholds, LabExecutor
from .labs.hosts import LocalLabHost, SshLabHost
from .memory.git_store import GitMemoryStore
from .notify.webhook import ChannelConfig, WebhookNotifier
from .resources import data_dir, defaults_dir
from .service import Adapters, Service, ServiceSettings
from .store.sqlite import SqliteStore
from .workspace.git import GitWorkspace, LocalSandbox

log = logging.getLogger(__name__)


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


async def build_service(cfg: HostConfig, dev: bool = False) -> Service:
    clock = SystemClock()
    data = cfg.data_path
    data.mkdir(parents=True, exist_ok=True)

    if cfg.github.auth == "app":
        if not (cfg.github.app_id and cfg.github.private_key_path):
            raise ValueError(
                "github.auth=app needs github.app_id and github.private_key_path"
            )
        pem = Path(cfg.github.private_key_path).expanduser().read_text(encoding="utf-8")
        tokens: Any = GitHubAppTokenProvider(
            cfg.github.app_id, pem, api_url=cfg.github.api_url
        )
    else:
        tokens = StaticTokenProvider(cfg.github.token_env)
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

    labs: dict[str, Any] = {}
    for name, lab in cfg.labs.items():
        labs[name] = (
            SshLabHost(name, lab.host or name)
            if lab.transport == "ssh"
            else LocalLabHost(name)
        )
    lab_runner = (
        LabExecutor(
            labs,
            {n: GpuThresholds(**lab.gpu.model_dump()) for n, lab in cfg.labs.items()},
            store,
            clock,
        )
        if labs
        else None
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

    adapters = Adapters(
        forge=reader,
        actuator_reader=fresh_reader,
        writer=writer,
        ci=ci,
        memory=memory,
        store=store,
        agents=router,
        workspace=GitWorkspace(data, token=tokens.token, forge_url=cfg.github.web_url),
        sandbox=LocalSandbox(),
        notifier=WebhookNotifier(channels),
        clock=clock,
        labs=labs,
        lab_runner=lab_runner,
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
        operators=set(cfg.operators),
        base_url=cfg.base_url,
        sandbox_repo=cfg.sandbox_repo,
    )
    return Service(adapters, settings)
