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

"""What a forge kind provides (design 5.16).

A ``ForgeKind`` describes one kind of forge (GitHub, GitLab) and builds the adapters
for one configured forge of that kind, a ``ForgeSite``, into its ``ForgeEndpoints``:
the ports core code uses, and what the composition root wires around them (the
service's token for git, a person's writer, the agents' read-only door).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.forges import ForgeInfo
from ..domain.ports import CiProvider, ForgeReader, ForgeWriter

TokenFn = Callable[[str | None], Awaitable[str | None]]
"""The service's token on a forge, for a repository (or none in particular)."""


@dataclass(frozen=True)
class ForgeSite:
    """One configured forge, as its kind's adapters need it."""

    id: str
    kind: str
    web_url: str
    api_url: str
    cli_config_dir: str | None
    data_dir: Path

    @property
    def host(self) -> str:
        return self.web_url.split("://", 1)[-1].split("/", 1)[0]


@dataclass
class ForgeEndpoints:
    """One forge's adapters, built by its kind."""

    site: ForgeSite
    info: ForgeInfo
    reader: ForgeReader
    fresh_reader: ForgeReader
    """Reads without the snapshot cache, for the actuator's checks before a write."""
    writer: ForgeWriter
    ci: CiProvider
    service_login: str
    """The login the service acts as there, as the forge spells it (not qualified)."""
    token: TokenFn
    git_user: str
    """The user name git presents with the token (``x-access-token``, ``oauth2``)."""
    writer_for_token: Callable[[str, str], ForgeWriter]
    """A writer acting as a person with their token: ``(token, login)``."""
    read_proxy: Any = None
    """The agents' read-only door to this forge, run alongside the service."""


@dataclass(frozen=True)
class ForgeKind:
    """One kind of forge: how to describe a site and build its adapters."""

    name: str
    describe: Callable[[ForgeSite], ForgeInfo]
    build: Callable[[ForgeSite], Awaitable[ForgeEndpoints]]
    verify_webhook: Callable[[bytes, bytes, str | None], bool] | None = None
    """Whether a delivery's body matches its signature header for a secret."""
    webhook_target: Callable[[str, dict[str, Any]], tuple[str, int | None] | None] | None = (
        None
    )
    """Which repository (as the forge names it) and change a delivery is about."""
