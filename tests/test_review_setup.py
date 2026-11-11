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

"""How a repository's PRs are reviewed (design 5.5): the skill its maintainers choose
among the repository's and the service's, their own review instructions, and the
question onboarding asks when the repository has no review skill."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.agents import AgentTask
from auto_maintainer.domain.memory import profile_path
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import RepoProfile
from auto_maintainer.playbooks.onboard import NO_REVIEW_SKILL
from auto_maintainer.playbooks.prompts import review_skill_block
from auto_maintainer.profiles.layers import parse_yaml
from auto_maintainer.resources import data_dir
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web import api as web_api
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
SKILL = "---\nname: {name}\ndescription: {what}\n---\n\n# {name}\n"


def _skills(root: Path, **folders: str) -> None:
    for folder, what in folders.items():
        path = root / ".claude" / "skills" / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL.format(name=folder, what=what), encoding="utf-8")


def _profile(**review: Any) -> RepoProfile:
    return RepoProfile.model_validate({"repo": REPO, "review": review})


def test_reviews_follow_the_skill_and_instructions_maintainers_chose(
    tmp_path: Path,
) -> None:
    _skills(tmp_path, **{"code-review": "How we review.", "release": "Cut a release."})
    skills = data_dir("skills")
    automatic = review_skill_block(_profile(), skills, tmp_path)
    assert "review skill `code-review`" in automatic, "its own review skill first"

    built_in = review_skill_block(_profile(skill="service:pr-review"), skills, tmp_path)
    assert str(skills / "pr-review" / "SKILL.md") in built_in
    assert "code-review" not in built_in, "the skill they chose against is not offered"
    assert ".claude/skills/release/SKILL.md" in built_in, "other skills stay usable"

    chosen = review_skill_block(
        _profile(skill="repo:.claude/skills/release/SKILL.md"), skills, tmp_path
    )
    assert "`.claude/skills/release/SKILL.md` in the working directory" in chosen
    assert "- `.claude/skills/release/SKILL.md`" not in chosen

    told = review_skill_block(
        _profile(instructions="Every new option needs docs."), skills, tmp_path
    )
    assert "Every new option needs docs." in told and "it wins" in told


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    web_api._SKILLS.clear()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_maintainers_choose_the_skill_and_say_how_to_review(demo) -> None:
    h, app = demo
    url = f"/api/repos/{REPO}/review-guides"
    async with _as(app, "maria") as maria:
        before = (await maria.get(url)).json()
        assert before["needs_choice"], "no review skill, and nobody chose yet"
        assert before["in_use"]["kind"] == "service"
        assert "pr-review" in [s["name"] for s in before["service_skills"]]

        h.forge.files[(REPO, ".agents/skills/triage-and-review/SKILL.md")] = (
            SKILL.format(name="triage-and-review", what="Our checklist.")
        )
        found = (await maria.get(url + "?refresh=true")).json()
        assert not found["needs_choice"], "the repository's own review skill"
        assert found["in_use"] == {
            "kind": "repo",
            "name": "triage-and-review",
            "path": ".agents/skills/triage-and-review/SKILL.md",
        }

        missing = await maria.put(url, json={"skill": "repo:../../etc/passwd"})
        assert missing.status_code == 422
        unknown = await maria.put(url, json={"skill": "service:nope"})
        assert unknown.status_code == 422

        saved = (
            await maria.put(
                url,
                json={
                    "skill": "service:pr-review",
                    "instructions": "Check that docs change with behavior.",
                },
            )
        ).json()
        assert saved["applied"], "a maintainer's change applies at once"
        layer = parse_yaml(h.memory.files[profile_path(REPO)], "settings")
        assert layer["review"]["skill"] == "service:pr-review"
        after = (await maria.get(url)).json()
        assert after["instructions"].strip() == "Check that docs change with behavior."
        assert after["in_use"]["kind"] == "service"

        # Choosing automatic again removes the key; instructions stay unless sent.
        await maria.put(url, json={"skill": after["default_skill"]})
        layer = parse_yaml(h.memory.files[profile_path(REPO)], "settings")
        assert "skill" not in layer["review"] and layer["review"]["instructions"]

    async with _as(app, "walt") as walt:
        proposed = (await walt.put(url, json={"instructions": ""})).json()
        assert not proposed["applied"], "a reviewer only proposes"


async def test_onboarding_asks_how_to_review_without_a_review_skill(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, approve=False)
    try:
        prompts: list[str] = []

        def onboard(task: AgentTask) -> dict[str, Any]:
            prompts.append(task.prompt)
            return {"profile": {"review": {"trigger": "manual"}}, "questions": []}

        h.runner.on("onboard", onboard)
        h.forge.files[(REPO, ".claude/skills/release/SKILL.md")] = SKILL.format(
            name="release", what="Cut a release."
        )
        await h.service.add_repo(REPO, "maria")
        await h.settle()
        [change] = await h.store.list_memory_changes()
        assert NO_REVIEW_SKILL.format(repo=REPO) in change.description
        assert ".claude/skills/release/SKILL.md" in prompts[-1], (
            "the agent knows the repository's skills"
        )
    finally:
        await h.close()

    h = await Harness.create(tmp_path / "again", approve=False)
    try:
        h.runner.on(
            "onboard",
            lambda task: {"profile": {"review": {"trigger": "manual"}}},
        )
        h.forge.files[(REPO, ".claude/skills/review/SKILL.md")] = SKILL.format(
            name="review", what="How we review."
        )
        await h.service.add_repo(REPO, "maria")
        await h.settle()
        [change] = await h.store.list_memory_changes()
        assert "no review skill" not in change.description
    finally:
        await h.close()


def test_reviews_read_no_lock_or_minified_files_line_by_line() -> None:
    from auto_maintainer.playbooks.prompts import without_generated

    diff = (
        "diff --git a/src/loader.py b/src/loader.py\n+    return []\n"
        "diff --git a/uv.lock b/uv.lock\n+" + "x" * 5000 + "\n"
        "diff --git a/ui/package-lock.json b/ui/package-lock.json\n+{}\n"
        "diff --git a/web/app.min.js b/web/app.min.js\n+!function(){}\n"
    )
    kept = without_generated(diff)
    assert (
        "+    return []" in kept and "x" * 100 not in kept and "!function" not in kept
    )
    assert kept.endswith(
        "[left out, generated or binary: uv.lock, ui/package-lock.json, web/app.min.js]\n"
    )
    assert without_generated("") == ""
