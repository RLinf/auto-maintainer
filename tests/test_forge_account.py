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

"""Account operations setup asks of each forge kind (design 5.16): one account's role
on a repository, whether it holds a commit, and making private repositories and
copies of the service's code."""

from __future__ import annotations

import json

import pytest
import respx

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.forges.base import AccountError
from auto_maintainer.forges.github import account as gh
from auto_maintainer.forges.gitlab import account as gl

GH = "https://api.github.com"
GL = "https://gitlab.example.com/api/v4"


def _gh_repo(full: str, **perms: bool) -> dict:
    return {
        "full_name": full,
        "private": False,
        "default_branch": "main",
        "html_url": f"https://github.com/{full}",
        "permissions": {"pull": True, **perms},
    }


async def test_github_reports_the_accounts_own_role() -> None:
    with respx.mock(base_url=GH) as mock:
        mock.get("/repos/alice/auto-maintainer").respond(
            200, json=_gh_repo("Alice/auto-maintainer", admin=True, push=True)
        )
        mock.get("/repos/bob/secret").respond(404)
        mine = await gh.repo(GH, "t", "alice/auto-maintainer")
        assert mine is not None
        assert (mine.path, mine.permission) == (
            "Alice/auto-maintainer",
            Permission.ADMIN,
        )
        assert await gh.repo(GH, "t", "bob/secret") is None


async def test_github_knows_whether_a_repository_holds_a_commit() -> None:
    with respx.mock(base_url=GH) as mock:
        mock.get("/repos/alice/am/commits/abc").respond(200, json={})
        mock.get("/repos/alice/am/commits/def").respond(422)
        assert await gh.has_commit(GH, "t", "alice/am", "abc") is True
        assert await gh.has_commit(GH, "t", "alice/am", "def") is False


async def test_github_creates_private_repositories_for_the_account_or_its_org() -> None:
    with respx.mock(base_url=GH) as mock:
        mock.get("/user").respond(200, json={"login": "alice"})
        mine = mock.post("/user/repos").respond(201, json={"full_name": "alice/mem"})
        org = mock.post("/orgs/acme/repos").respond(201, json={"full_name": "acme/mem"})
        mock.get("/repos/alice/mem").respond(
            200, json=_gh_repo("alice/mem", admin=True)
        )
        mock.get("/repos/acme/mem").respond(200, json=_gh_repo("acme/mem", admin=True))
        await gh.create_private(GH, "t", "alice/mem", "memory")
        await gh.create_private(GH, "t", "acme/mem", "memory")
        assert json.loads(mine.calls[0].request.content)["private"] is True
        assert org.called


async def test_github_forks_only_on_the_same_github() -> None:
    with respx.mock(base_url=GH) as mock:
        mock.post("/repos/RLinf/auto-maintainer/forks").respond(
            202, json={"full_name": "alice/auto-maintainer"}
        )
        mock.get("/repos/alice/auto-maintainer").respond(
            200, json=_gh_repo("alice/auto-maintainer", admin=True)
        )
        made = await gh.copy(
            GH, "t", "https://github.com/RLinf/auto-maintainer", "RLinf/auto-maintainer"
        )
        assert made.path == "alice/auto-maintainer"
    with pytest.raises(AccountError, match="same GitHub"):
        await gh.copy(GH, "t", "https://gitlab.com/x/auto-maintainer", None)


def _gl_project(path: str, level: int, visibility: str = "private") -> dict:
    return {
        "id": 7,
        "path_with_namespace": path,
        "visibility": visibility,
        "default_branch": "main",
        "web_url": f"https://gitlab.example.com/{path}",
        "permissions": {
            "project_access": None,
            "group_access": {"access_level": level},
        },
    }


async def test_gitlab_reports_the_accounts_role_from_project_or_group() -> None:
    with respx.mock(base_url=GL) as mock:
        mock.get("/projects/team%2Fauto-maintainer").respond(
            200, json=_gl_project("team/auto-maintainer", 50)
        )
        mock.get("/projects/team%2Fhidden").respond(404)
        found = await gl.repo(GL, "t", "team/auto-maintainer")
        assert found is not None and found.permission is Permission.ADMIN
        assert found.private is True
        assert await gl.repo(GL, "t", "team/hidden") is None


async def test_gitlab_imports_code_from_another_forge_privately() -> None:
    with respx.mock(base_url=GL) as mock:
        made = mock.post("/projects").respond(
            201, json=_gl_project("alice/auto-maintainer", 50)
        )
        mock.get("/projects/7/import").respond(200, json={"import_status": "finished"})
        mock.get("/projects/alice%2Fauto-maintainer").respond(
            200, json=_gl_project("alice/auto-maintainer", 50)
        )
        copy = await gl.copy(GL, "t", "https://github.com/RLinf/auto-maintainer", None)
        assert copy.path == "alice/auto-maintainer"
        body = json.loads(made.calls[0].request.content)
        assert body["import_url"] == "https://github.com/RLinf/auto-maintainer.git"
        assert body["visibility"] == "private"


async def test_gitlab_creates_private_projects_in_a_group() -> None:
    with respx.mock(base_url=GL) as mock:
        mock.get("/user").respond(200, json={"username": "alice"})
        mock.get("/namespaces/team%2Fsub").respond(200, json={"id": 42})
        made = mock.post("/projects").respond(
            201, json=_gl_project("team/sub/memory", 40)
        )
        mock.get("/projects/team%2Fsub%2Fmemory").respond(
            200, json=_gl_project("team/sub/memory", 40)
        )
        found = await gl.create_private(GL, "t", "team/sub/memory", "memory")
        assert found.permission is Permission.MAINTAIN
        assert json.loads(made.calls[0].request.content)["namespace_id"] == 42


async def test_gitlab_says_when_a_copy_failed() -> None:
    with respx.mock(base_url=GL) as mock:
        mock.post("/projects/up%2Fam/fork").respond(
            201, json=_gl_project("alice/am", 50)
        )
        mock.get("/projects/7/import").respond(
            200, json={"import_status": "failed", "import_error": "quota"}
        )
        with pytest.raises(AccountError, match="quota"):
            await gl.copy(GL, "t", "https://gitlab.example.com/up/am", "up/am")
