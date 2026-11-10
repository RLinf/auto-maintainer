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
from ..domain.permissions import Permission
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
class RepoAccess:
    """What one account sees of a repository on its forge, for setting a host up."""

    path: str
    """Its full path on the forge (``owner/name``, ``group/sub/name``)."""
    private: bool
    permission: Permission
    """The account's own role there."""
    default_branch: str
    web_url: str


class AccountError(RuntimeError):
    """A forge refused or failed an account operation; the message says why."""


@dataclass(frozen=True)
class ForgeAccount:
    """What setting a host up asks of a forge, each with one account's token: who it
    is, what it sees of a repository, and making repositories (design 5.16)."""

    repo: Callable[[str, str, str], Awaitable[RepoAccess | None]]
    """``(api_url, token, path)``: the repository as that account sees it; ``None``
    when it does not exist or the account cannot see it."""
    has_commit: Callable[[str, str, str, str], Awaitable[bool]]
    """``(api_url, token, path, sha)``: whether the repository holds that commit."""
    create_private: Callable[[str, str, str, str], Awaitable[RepoAccess]]
    """``(api_url, token, path, description)``: a new private repository, owned by
    the account or a namespace it may create in; raises ``AccountError``."""
    copy: Callable[[str, str, str, str | None], Awaitable[RepoAccess]]
    """``(api_url, token, source_web_url, source_path)``: the account's own copy of a
    repository, a fork when ``source_path`` names one on the same forge, else an
    import of ``source_web_url``; raises ``AccountError``."""


@dataclass(frozen=True)
class ForgeKind:
    """One kind of forge: how to describe a site and build its adapters."""

    name: str
    title: str
    """How people call it: GitHub, GitLab."""
    describe: Callable[[ForgeSite], ForgeInfo]
    build: Callable[[ForgeSite], Awaitable[ForgeEndpoints]]
    verify_webhook: Callable[[bytes, bytes, str | None], bool] | None = None
    """Whether a delivery's body matches its signature header for a secret."""
    webhook_target: (
        Callable[[str, dict[str, Any]], tuple[str, int | None] | None] | None
    ) = None
    """Which repository (as the forge names it) and change a delivery is about."""
    who: Callable[[str, str], Awaitable[dict[str, str]]] | None = None
    """``(api_url, token)`` to the token's person: ``login`` as the forge spells it,
    ``name``, ``avatar_url``; raises when the token does not work."""
    webhook_headers: tuple[str, str] = ("X-Hub-Signature-256", "X-GitHub-Event")
    """The headers carrying a delivery's signature (or token) and its event name."""
    account: ForgeAccount | None = None
    """Account operations for the setup page and the owner's settings."""
