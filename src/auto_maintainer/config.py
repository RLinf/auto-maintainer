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

"""Host configuration: ``local.yaml`` and ``secrets.env`` (design 7.3).

Host configuration says where things are and which credentials to use. It never
decides how the service behaves on a repository; repository profiles do. The setup
page writes both files (``save_host_config``, ``set_host_secret``); nobody needs to
edit them by hand.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_DIR = Path(
    os.environ.get("AM_CONFIG_DIR", Path.home() / ".config" / "auto-maintainer")
)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitHubConfig(_Cfg):
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    gh_config_dir: str | None = None
    """The GitHub CLI login the service acts as, made on the setup page; unset, the
    host user's own GitHub CLI login is used."""
    webhook_secret_env: str = "AM_WEBHOOK_SECRET"


LEGACY_GITHUB_KEYS = (
    "auth",
    "token_env",
    "app_id",
    "app_slug",
    "private_key_path",
    "oauth_client_id",
    "oauth_client_secret_env",
)
"""``github`` keys of the GitHub App and token setups, which no longer exist. The
loader drops them; ``legacy_github`` says whether the host must connect again."""

LEGACY_SECRETS = ("GITHUB_TOKEN", "AM_OAUTH_CLIENT_SECRET")
"""What those setups kept in ``secrets.env``; a new setup removes them."""


class MemoryConfig(_Cfg):
    path: str | None = None
    remote: str | None = None
    """``owner/repo`` of the private memory repo; ``None`` keeps memory local."""
    branch: str = "main"


class GpuThresholdConfig(_Cfg):
    max_mem_mib: int = 500
    max_util_pct: int = 0


class LabHostConfig(_Cfg):
    transport: Literal["ssh", "local"] = "ssh"
    host: str | None = None
    gpu: GpuThresholdConfig = Field(default_factory=GpuThresholdConfig)


class SelfUpdateConfig(_Cfg):
    """How the service changes and updates its own code (design 5.12)."""

    branch: str = "main"
    """The default branch of ``code_repo``; deploys only go to commits on it."""
    approve_permission: Literal["write", "maintain", "admin"] = "maintain"
    """Permission on ``code_repo`` needed to merge the service's own PRs and deploy."""
    checkout: str | None = None
    """The git checkout the service runs from; defaults to the installed source tree."""
    remote: str = "origin"
    test_command: list[str] | None = None
    """Run in a staging worktree before a deploy; defaults to the unit tests."""


class HostConfig(_Cfg):
    host: str = "0.0.0.0"
    """Address the dashboard and API listen on; ``0.0.0.0`` is every interface."""
    port: int = 7777
    base_url: str = "http://127.0.0.1:7777"
    """The URL browsers use to reach the dashboard. It builds the links in alerts and
    sign-in links, so set it to the host's reachable name or address when people
    connect from other machines."""
    data_dir: str = os.environ.get(
        "AM_DATA_DIR", str(Path.home() / ".local" / "share" / "auto-maintainer")
    )
    operators: list[str] = Field(default_factory=list)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    code_repo: str | None = None
    """``owner/repo`` of the service's own code, where self-improvements go."""
    default_agent: str | None = None
    """The agent chosen during setup to run every task first; the service settings'
    ``default_agent`` overrides it."""
    self_update: SelfUpdateConfig = Field(default_factory=SelfUpdateConfig)
    labs: dict[str, LabHostConfig] = Field(default_factory=dict)
    host_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per repository: host paths for its labs (design 2.2, layer 5)."""
    pools: dict[str, int] = Field(
        default_factory=lambda: {"io": 8, "agent": 3, "lab": 1}
    )
    poll_seconds: int = 120
    write_caps: tuple[int, int] = (120, 40)
    sandbox_repo: str | None = None
    """The only repository a development instance (``am serve --dev``) serves. Its
    actions follow its profile; every other repository is refused. Without it, dev
    mode serves every repository but holds all their actions at shadow."""
    cookie_secure: bool = False

    @property
    def data_path(self) -> Path:
        return Path(os.path.expanduser(self.data_dir))

    @property
    def memory_path(self) -> Path:
        return (
            Path(os.path.expanduser(self.memory.path))
            if self.memory.path
            else self.data_path / "memory"
        )


def load_secrets(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into the environment without overriding set variables."""
    if not path.is_file():
        return
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must be readable only by its owner (chmod 600)")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read(root: Path) -> dict[str, Any]:
    path = root / "local.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return data or {}


def load_host_config(config_dir: Path | None = None) -> HostConfig:
    """``local.yaml`` as a ``HostConfig``, without the keys of the removed GitHub App
    and token setups (``legacy_github`` says whether they mattered)."""
    root = config_dir or CONFIG_DIR
    load_secrets(root / "secrets.env")
    data = _read(root)
    github = data.get("github")
    if isinstance(github, dict):
        data["github"] = {
            k: v for k, v in github.items() if k not in LEGACY_GITHUB_KEYS
        }
    return HostConfig.model_validate(data)


def legacy_github(config_dir: Path | None = None) -> str | None:
    """How the host acted on GitHub before the GitHub CLI became the only way, when
    that was not the GitHub CLI: ``"a GitHub App"`` or ``"a token"``; else ``None``.

    Such a host must connect the GitHub CLI on the setup page before the service
    starts, so that it never switches silently to whatever account the host's own
    GitHub CLI is logged in to. A host that named no way and kept no token in
    ``secrets.env`` was already reading the GitHub CLI's token.
    """
    root = config_dir or CONFIG_DIR
    github = _read(root).get("github") or {}
    auth = github.get("auth")
    if auth == "app" or github.get("app_id"):
        return "a GitHub App"
    if auth == "gh":
        return None
    token_env = github.get("token_env") or "GITHUB_TOKEN"
    secrets = root / "secrets.env"
    kept = {
        line.partition("=")[0].strip()
        for line in (
            secrets.read_text(encoding="utf-8").splitlines()
            if secrets.is_file()
            else []
        )
    }
    return "a token" if auth == "token" or token_env in kept else None


def is_configured(config_dir: Path | None = None) -> bool:
    """Whether setup has written ``local.yaml``; without it ``am serve`` runs setup."""
    return ((config_dir or CONFIG_DIR) / "local.yaml").is_file()


HEADER = (
    "# Written by auto-maintainer. Change these on the dashboard (Settings, then\n"
    "# System) rather than here; the service rewrites this file.\n"
)


def _write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_host_config(cfg: HostConfig, config_dir: Path | None = None) -> Path:
    """Write ``local.yaml`` atomically, keeping only what differs from the defaults."""
    root = config_dir or CONFIG_DIR
    data = cfg.model_dump(mode="json", exclude_defaults=True)
    path = root / "local.yaml"
    _write_private(
        path, HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), 0o644
    )
    return path


def set_host_secret(name: str, value: str, config_dir: Path | None = None) -> None:
    """Set ``name`` in ``secrets.env`` (mode 0600) and in this process's environment."""
    root = config_dir or CONFIG_DIR
    path = root / "secrets.env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept = [ln for ln in lines if ln.partition("=")[0].strip() != name]
    _write_private(path, "\n".join([*kept, f"{name}={value}"]) + "\n")
    os.environ[name] = value


def remove_host_secret(name: str, config_dir: Path | None = None) -> None:
    """Remove ``name`` from ``secrets.env`` and from this process's environment."""
    root = config_dir or CONFIG_DIR
    path = root / "secrets.env"
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if ln.partition("=")[0].strip() != name]
        if kept != lines:
            _write_private(path, "\n".join(kept) + ("\n" if kept else ""))
    os.environ.pop(name, None)


def write_private_file(path: Path, text: str) -> None:
    """Write a file only its owner can read, such as the setup code."""
    _write_private(path, text)


EXAMPLE = """# ~/.config/auto-maintainer/local.yaml
port: 7777
host: 0.0.0.0                           # listen on every interface
base_url: http://your-host:7777         # what browsers open; links in alerts point here
operators: [your-login]
github:
  gh_config_dir: ~/.config/auto-maintainer/gh   # the GitHub CLI login the service acts as
memory:
  remote: your-org/auto-maintainer-memory
code_repo: your-org/auto-maintainer     # the service's own code; reviewers' requests become PRs here
self_update:
  approve_permission: maintain          # needed on code_repo to merge those PRs and to deploy
# sandbox_repo: your-org/sandbox        # am serve --dev then serves only this repository
labs:
  gpu-host: {transport: ssh, host: gpu-host}
host_overrides:
  your-org/your-repo:
    labs:
      gpu-host: {base_clone: /srv/lab/your-repo, worktree_root: /srv/lab/your-repo-worktrees}
"""
