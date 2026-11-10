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

"""GitLab as a forge kind (design 5.16): gitlab.com or a self-managed instance,
reached as the GitLab CLI's (``glab``) login."""

from __future__ import annotations

from ...domain.forges import ForgeCapabilities, ForgeInfo, ForgeTerms
from ..base import ForgeEndpoints, ForgeKind, ForgeSite
from .account import ACCOUNT
from .ci import GitLabCi
from .client import GitLabClient, GlabToken
from .reader import GitLabReader
from .webhooks import hint, verify_token
from .writer import GitLabWriter


def describe(site: ForgeSite) -> ForgeInfo:
    return ForgeInfo(
        id=site.id,
        kind="gitlab",
        web_url=site.web_url.rstrip("/"),
        capabilities=ForgeCapabilities(
            draft=True,
            requested_reviewers=True,
            blocking_verdict=False,
            held_ci=False,
            update_branch="rebase",
            suggestions="```suggestion:-0+0",
        ),
        terms=ForgeTerms(
            change="merge request",
            change_short="MR",
            change_number_prefix="!",
            forge_name="GitLab",
        ),
        codeowners=("CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"),
        change_url="{web}/{path}/-/merge_requests/{number}",
        file_url="{web}/{path}/-/blob/{ref}/{file}",
        tree_url="{web}/{path}/-/tree/{ref}/{file}",
        contributors_url="{web}/{path}/-/project_members",
        user_url="{web}/{login}",
        avatar_url="",
    )


async def build(site: ForgeSite) -> ForgeEndpoints:
    """The service acts as the GitLab CLI login made on the setup page (its own
    ``GLAB_CONFIG_DIR``), or, without one, the host user's own ``glab`` login."""
    token = GlabToken(site.host, site.cli_config_dir)
    client = GitLabClient(site.api_url, token.token)
    reader = GitLabReader(client, site.id)

    def writer_for_token(person_token: str, login: str) -> GitLabWriter:
        async def fixed() -> str:
            return person_token

        return GitLabWriter(
            GitLabClient(site.api_url, fixed), site.web_url, person=login
        )

    return ForgeEndpoints(
        site=site,
        info=describe(site),
        reader=reader,
        fresh_reader=GitLabReader(client, site.id, snapshot_cache_seconds=0),
        writer=GitLabWriter(client, site.web_url),
        ci=GitLabCi(client),
        service_login=await reader.service_login(),
        token=lambda repo: token.token(repo),
        git_user="oauth2",
        writer_for_token=writer_for_token,
        read_proxy=None,
    )


async def who(api_url: str, token: str) -> dict[str, str]:
    async def fixed() -> str:
        return token

    client = GitLabClient(api_url, fixed)
    try:
        data = await client.get("/user")
    finally:
        await client.aclose()
    return {
        "login": str(data["username"]),
        "name": str(data.get("name") or data["username"]),
        "avatar_url": str(data.get("avatar_url") or ""),
    }


GITLAB = ForgeKind(
    name="gitlab",
    title="GitLab",
    describe=describe,
    build=build,
    verify_webhook=verify_token,
    webhook_target=hint,
    who=who,
    webhook_headers=("X-Gitlab-Token", "X-Gitlab-Event"),
    account=ACCOUNT,
)
