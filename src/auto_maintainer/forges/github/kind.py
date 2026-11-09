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

"""GitHub as a forge kind (design 5.16): github.com or GitHub Enterprise Server,
reached as the GitHub CLI's login."""

from __future__ import annotations

from ...domain.forges import ForgeCapabilities, ForgeInfo, ForgeTerms
from ..base import ForgeEndpoints, ForgeKind, ForgeSite
from .auth import StaticTokenProvider
from .ci import GitHubActionsCi
from .client import GitHubClient
from .proxy import GitHubReadProxy
from .reader import GitHubReader
from .webhooks import hint, verify_signature
from .writer import GitHubWriter

PUBLIC_WEB = "https://github.com"


def describe(site: ForgeSite) -> ForgeInfo:
    web = site.web_url.rstrip("/")
    return ForgeInfo(
        id=site.id,
        kind="github",
        web_url=web,
        capabilities=ForgeCapabilities(
            draft=True,
            requested_reviewers=True,
            blocking_verdict=True,
            held_ci=True,
            update_branch="merge",
            suggestions="```suggestion",
        ),
        terms=ForgeTerms(
            change="pull request",
            change_short="PR",
            change_number_prefix="#",
            forge_name="GitHub",
        ),
        codeowners=(".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"),
        change_url="{web}/{path}/pull/{number}",
        file_url="{web}/{path}/blob/{ref}/{file}",
        tree_url="{web}/{path}/tree/{ref}/{file}",
        contributors_url="{web}/{path}/graphs/contributors",
        user_url="{web}/{login}",
        avatar_url="https://avatars.githubusercontent.com/{login}?s={size}"
        if web == PUBLIC_WEB
        else "{web}/{login}.png?size={size}",
    )


async def build(site: ForgeSite) -> ForgeEndpoints:
    """The service acts as the GitHub CLI login made on the setup page, or, without
    one, the account the host user's own GitHub CLI is logged in to."""
    tokens = StaticTokenProvider(None, gh_config_dir=site.cli_config_dir)
    client = GitHubClient(tokens, api_url=site.api_url)
    reader = GitHubReader(client)
    socket = site.data_dir / "sandbox" / (
        "github.sock" if site.id == "github" else f"{site.id}.sock"
    )
    api_host = site.api_url.split("://", 1)[-1].split("/", 1)[0]

    def writer_for_token(token: str, login: str) -> GitHubWriter:
        return GitHubWriter(
            GitHubClient(StaticTokenProvider(token=token), api_url=site.api_url),
            person=login,
        )

    return ForgeEndpoints(
        site=site,
        info=describe(site),
        reader=reader,
        fresh_reader=GitHubReader(client, snapshot_cache_seconds=0),
        writer=GitHubWriter(client),
        ci=GitHubActionsCi(client),
        service_login=await reader.service_login(),
        token=tokens.token,
        git_user="x-access-token",
        writer_for_token=writer_for_token,
        read_proxy=GitHubReadProxy(socket, lambda: tokens.token(None), api_host=api_host),
    )


GITHUB = ForgeKind(
    name="github",
    describe=describe,
    build=build,
    verify_webhook=verify_signature,
    webhook_target=hint,
)
