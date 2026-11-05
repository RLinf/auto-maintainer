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
decides how the service behaves on a repository; repository profiles do.
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
    auth: Literal["token", "app"] = "token"
    token_env: str = "GITHUB_TOKEN"
    app_id: int | None = None
    private_key_path: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret_env: str = "AM_OAUTH_CLIENT_SECRET"
    webhook_secret_env: str = "AM_WEBHOOK_SECRET"


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


class HostConfig(_Cfg):
    host: str = "127.0.0.1"
    port: int = 7777
    base_url: str = "http://127.0.0.1:7777"
    data_dir: str = str(Path.home() / ".local" / "share" / "auto-maintainer")
    operators: list[str] = Field(default_factory=list)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    code_repo: str | None = None
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


def load_host_config(config_dir: Path | None = None) -> HostConfig:
    root = config_dir or CONFIG_DIR
    load_secrets(root / "secrets.env")
    path = root / "local.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return HostConfig.model_validate(data or {})


EXAMPLE = """# ~/.config/auto-maintainer/local.yaml
port: 7777
base_url: http://127.0.0.1:7777
operators: [your-login]
github:
  auth: token            # or app, with app_id and private_key_path
  token_env: GITHUB_TOKEN
  oauth_client_id: Iv1.xxxxx
memory:
  remote: your-org/auto-maintainer-memory
labs:
  gpu-host: {transport: ssh, host: gpu-host}
host_overrides:
  your-org/your-repo:
    labs:
      gpu-host: {base_clone: /srv/lab/your-repo, worktree_root: /srv/lab/your-repo-worktrees}
"""
