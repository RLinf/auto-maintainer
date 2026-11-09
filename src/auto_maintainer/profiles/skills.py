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

"""The skills a repository's reviews can follow (design 5.5): the repository's own,
read from its default branch through the forge, and the service's."""

from __future__ import annotations

import re
from pathlib import Path

from ..domain.agents import REPO_SKILL_FOLDERS, SkillInfo, parse_skill
from ..domain.ports import ForgeReader
from ..domain.profile import RepoProfile

MAX_SKILLS = 60
"""The most of a repository's skills read; each one is a file read from the forge."""

_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


async def repository_skills(
    forge: ForgeReader, repo: str, ref: str | None = None
) -> list[SkillInfo]:
    """The repository's own skills on ``ref`` (its default branch when ``None``), in
    the order a review looks for them."""
    found: list[SkillInfo] = []
    for base in REPO_SKILL_FOLDERS:
        for folder in sorted(await forge.list_dir(repo, base, ref)):
            if len(found) >= MAX_SKILLS:
                return found
            path = f"{base}/{folder}/SKILL.md"
            text = await forge.get_file(repo, path, ref)
            if text:
                found.append(parse_skill(text, folder, path))
    return found


def service_skills(skills_dir: Path) -> list[SkillInfo]:
    """The service's own review skills: those in ``skills_dir`` whose name says review."""
    out = []
    for skill in sorted(skills_dir.glob("*/SKILL.md")):
        info = parse_skill(
            skill.read_text(encoding="utf-8", errors="replace"), skill.parent.name
        )
        if "review" in skill.parent.name:
            out.append(info.model_copy(update={"name": skill.parent.name}))
    return out


def review_skill_in_use(
    profile: RepoProfile, own: list[SkillInfo]
) -> tuple[str, SkillInfo | None]:
    """What a review follows under the profile, given the repository's skills:
    (``repo``, the skill) or (``service``, None) for the service's skill the
    profile names, or (``missing``, None) when it names a repository skill that is
    not there."""
    skill = profile.review.skill
    if skill.startswith("repo:"):
        path = skill.split(":", 1)[1].split("#", 1)[0]
        named = next((s for s in own if s.path == path), None)
        return ("repo", named) if named else ("missing", None)
    if skill.startswith("builtin:"):
        mine = next((s for s in own if s.reviews), None)
        if mine:
            return "repo", mine
    return "service", None


def check_skill_choice(
    value: str, own: list[SkillInfo], service: list[SkillInfo]
) -> str | None:
    """Why ``value`` cannot be a repository's ``review.skill``, or None when it can:
    a skill of the service by name, or one of the repository's skills by path."""
    kind, _, rest = value.partition(":")
    if kind in ("builtin", "service"):
        if not _SERVICE_NAME.match(rest) or rest not in {s.name for s in service}:
            return f"the service has no review skill named {rest!r}"
        return None
    if kind == "repo":
        if rest not in {s.path for s in own}:
            return f"the repository has no skill at {rest!r} on its default branch"
        return None
    return "a review skill is builtin:<name>, service:<name>, or repo:<path>"
