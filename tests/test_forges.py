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

"""Names on several forges (design 5.16): repositories and people, where they live in
the memory repo and on the host, and how the API takes them."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from auto_maintainer.domain import forges
from auto_maintainer.domain.memory import is_configuration_path, repo_dir, repos_of_path
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


@pytest.fixture
def two_forges(monkeypatch):
    monkeypatch.setattr(
        forges, "_forges", forges._Forges("github", frozenset({"github", "corp"}))
    )


def test_names_on_the_default_forge_stay_as_they_were(two_forges) -> None:
    assert forges.check_repo("RLinf/RLinf") == "RLinf/RLinf"
    assert forges.check_repo("github:RLinf/RLinf") == "RLinf/RLinf"
    ref = forges.parse_repo("corp:infra/tools/builder")
    assert (ref.forge, ref.path, ref.namespace, ref.project) == (
        "corp",
        "infra/tools/builder",
        "infra/tools",
        "builder",
    )
    assert ref.name == "corp:infra/tools/builder"
    for bad in ("RLinf", "other:a/b", "corp:a/../b", "corp:/a/b", "a/b/", "Corp:a/b"):
        with pytest.raises(ValueError):
            forges.check_repo(bad)


def test_each_repository_has_its_own_memory_folder(two_forges) -> None:
    assert repo_dir("RLinf/RLinf") == "repos/RLinf/RLinf"
    nested = repo_dir("corp:infra/tools/builder")
    assert nested == "forges/corp/infra/tools/builder/-"
    for name in ("RLinf/RLinf", "corp:infra/tools/builder", "corp:infra/knowledge"):
        path = f"{repo_dir(name)}/knowledge/review_lessons.md"
        assert repos_of_path(path) == [name]
    assert is_configuration_path(f"{nested}/profile.yaml")
    assert not is_configuration_path(f"{nested}/knowledge/x.md")
    assert repos_of_path("config/service.yaml") == []
    assert repo_dir("service") == "repos/service", "the service's own changes"


def test_host_folders_and_file_names_never_collide(two_forges) -> None:
    assert forges.fs_parts("RLinf/RLinf") == ("RLinf", "RLinf")
    assert forges.fs_parts("corp:RLinf/RLinf") == ("@corp", "RLinf", "RLinf")
    assert forges.slug("corp:a/b/c") == "corp~a__b__c"
    assert forges.slug("a/b") != forges.slug("corp:a/b")


def test_a_login_means_something_only_on_its_own_forge(two_forges) -> None:
    assert forges.check_person("@alice") == "alice"
    assert forges.check_person("corp:alice.b") == "corp:alice.b"
    assert forges.person_on("github", "alice") == "alice"
    assert forges.person_on("corp", "alice") == "corp:alice"
    assert forges.check_person("alice") != forges.check_person("corp:alice")
    with pytest.raises(ValueError):
        forges.check_person("nowhere:alice")


async def test_the_api_takes_a_nested_name_as_one_repository(
    two_forges, tmp_path: Path
) -> None:
    h = await build_demo(tmp_path)
    try:
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as maria:
            await maria.get("/auth/dev?login=maria")
            usage = await maria.get("/api/repos/corp:infra/tools/builder/usage")
            pr = await maria.get("/api/prs/corp:infra/tools/builder/42")
            known = await maria.get("/api/prs/acme/widget/42")
            unknown_forge = await maria.get("/api/repos/other:a/b/usage")
        assert (
            usage.status_code in (403, 404)
            and usage.json().get("detail") != "Not Found"
        )
        assert pr.status_code in (403, 404) and pr.json().get("detail") != "Not Found"
        assert known.status_code == 200
        assert unknown_forge.status_code in (400, 404, 422)
    finally:
        await h.close()


class _Reader:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []

    async def get_pr(self, repo: str, number: int) -> str:
        self.calls.append((repo, number))
        return f"{self.name}:{number}"

    async def service_login(self) -> str:
        return f"bot-{self.name}"


async def test_the_router_hands_each_call_to_its_repositorys_forge(two_forges) -> None:
    from auto_maintainer.forges.router import ForgeRouter

    hub, corp = _Reader("hub"), _Reader("corp")
    router = ForgeRouter({"github": hub, "corp": corp})
    assert await router.get_pr("RLinf/RLinf", 1) == "hub:1"
    assert await router.get_pr("corp:infra/tools/builder", 2) == "corp:2"
    assert corp.calls == [("corp:infra/tools/builder", 2)]
    assert await router.service_login() == "bot-hub"
    with pytest.raises(ValueError):
        await router.get_pr("nowhere:a/b", 3)


def test_each_forge_keeps_its_own_codeowners_order(monkeypatch) -> None:
    from auto_maintainer.domain.codeowners import codeowners_locations

    gitlab = forges.ForgeInfo(
        id="corp",
        kind="gitlab",
        codeowners=("CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"),
    )
    monkeypatch.setattr(
        forges,
        "_forges",
        forges._Forges(
            "github",
            frozenset({"github", "corp"}),
            {"github": forges.ForgeInfo(id="github"), "corp": gitlab},
        ),
    )
    assert codeowners_locations("RLinf/RLinf")[0] == ".github/CODEOWNERS"
    assert codeowners_locations("corp:a/b")[-1] == ".gitlab/CODEOWNERS"
    assert forges.info_of("corp:alice").kind == "gitlab"


async def test_the_dashboard_learns_the_forges(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as maria:
            await maria.get("/auth/dev?login=maria")
            view = (await maria.get("/api/forges")).json()
        [github] = view["forges"]
        assert view["default"] == "github" and github["kind"] == "github"
        assert github["change_url"] == "{web}/{path}/pull/{number}"
    finally:
        await h.close()
