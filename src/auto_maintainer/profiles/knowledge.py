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

"""Typed reads of learned knowledge in the memory repo (design 7.2).

Knowledge is not configuration: stage goals, label rules, flake signatures, review
lessons, and internal entity lists can change through T1 memory changes.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..domain.memory import repo_dir
from ..domain.ports import MemoryStore
from ..domain.profile import FlakeSignature, LabelRule, RepoProfile
from ..domain.scoring import StageGoal

STAGE_GOALS = "rubric/stage_goals.yaml"
LABEL_RULES = "knowledge/labels.yaml"
FLAKES = "ci/flake_signatures.yaml"
LESSONS = "knowledge/review_lessons.md"
GLOBAL_FLAKES = "global/ci/flake_signatures.yaml"
GLOBAL_LESSONS = "global/knowledge/review_lessons.md"
AGENT_LESSONS = "knowledge/agent_lessons.md"
"""What the service's agents learned from their own trouble doing work in one
repository (``playbooks/retrospect.py``), as its maintainers approved it."""
GLOBAL_AGENT_LESSONS = "global/knowledge/agent_lessons.md"
"""The same about the service's own tools, as its owner approved it."""
MACHINE_LESSONS = "global/knowledge/machines/{name}.md"
"""The same about one test machine, as the service's owner approved it."""


def machine_lessons_path(name: str) -> str:
    """Where the lessons about test machine ``name`` live in the memory repo."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip(".")
    return MACHINE_LESSONS.format(name=safe or "machine")


class Knowledge:
    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    async def _yaml(self, path: str) -> Any:
        text = await self._memory.read_text(path)
        if not text:
            return None
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None

    async def stage_goals(self, repo: str, profile: RepoProfile) -> list[StageGoal]:
        path = profile.priority.stage_goals or STAGE_GOALS
        data = await self._yaml(f"{repo_dir(repo)}/{path}")
        items = data.get("stage_goals", data) if isinstance(data, dict) else data
        return [StageGoal.model_validate(g) for g in items or []]

    async def label_rules(self, repo: str, profile: RepoProfile) -> list[LabelRule]:
        rules = profile.labels.rules
        if isinstance(rules, list):
            return rules
        data = await self._yaml(f"{repo_dir(repo)}/{rules or LABEL_RULES}")
        items = data.get("rules", data) if isinstance(data, dict) else data
        return [LabelRule.model_validate(r) for r in items or []]

    async def internal_entities(self, repo: str, profile: RepoProfile) -> list[str]:
        entities = profile.confidentiality.internal_entities
        if isinstance(entities, list):
            return entities
        data = await self._yaml(f"{repo_dir(repo)}/{entities}")
        if isinstance(data, dict):
            names: list[str] = []
            for value in data.values():
                if isinstance(value, list):
                    names += [
                        v if isinstance(v, str) else str(v.get("name", ""))
                        for v in value
                    ]
            return [n for n in names if n]
        return [str(x) for x in data or []]

    async def flake_signatures(
        self, repo: str, profile: RepoProfile
    ) -> list[FlakeSignature]:
        found = list(profile.ci.flake_signatures)
        paths = [f"{repo_dir(repo)}/{FLAKES}"]
        if profile.memory.use_global:
            paths.append(GLOBAL_FLAKES)
        seen = {s.id for s in found}
        for path in paths:
            data = await self._yaml(path)
            items = data.get("signatures", data) if isinstance(data, dict) else data
            for item in items or []:
                sig = FlakeSignature.model_validate(item)
                if sig.id not in seen:
                    found.append(sig)
                    seen.add(sig.id)
        return found

    async def review_lessons(self, repo: str, profile: RepoProfile) -> str:
        parts = [await self._memory.read_text(f"{repo_dir(repo)}/{LESSONS}") or ""]
        if profile.memory.use_global:
            parts.append(await self._memory.read_text(GLOBAL_LESSONS) or "")
        return "\n\n".join(p.strip() for p in parts if p.strip())

    async def agent_lessons(
        self, repo: str | None, machines: list[str] | tuple[str, ...] = ()
    ) -> str:
        """What the service's agents learned from earlier trouble, as approved: about
        ``repo``, about each of ``machines``, and about the service's own tools;
        each part under its own heading, empty when none has any."""
        parts: list[tuple[str, str]] = []
        if repo:
            parts.append(
                (
                    f"About work in {repo}",
                    await self._memory.read_text(f"{repo_dir(repo)}/{AGENT_LESSONS}")
                    or "",
                )
            )
        for name in machines:
            parts.append(
                (
                    f"About test machine {name}",
                    await self._memory.read_text(machine_lessons_path(name)) or "",
                )
            )
        parts.append(
            (
                "About the service's own tools",
                await self._memory.read_text(GLOBAL_AGENT_LESSONS) or "",
            )
        )
        return "\n\n".join(
            f"## {title}\n{_items(text)}" for title, text in parts if _items(text)
        )

    async def memory_index(self, repo: str | None) -> str:
        parts = [await self._memory.read_text("MEMORY.md") or ""]
        if repo:
            parts.append(
                await self._memory.read_text(f"{repo_dir(repo)}/MEMORY.md") or ""
            )
        return "\n\n".join(p.strip() for p in parts if p.strip())


def _items(text: str) -> str:
    """The list items of a lessons file, without its heading."""
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ).strip()


def dump_stage_goals(goals: list[StageGoal]) -> str:
    return yaml.safe_dump(
        {"stage_goals": [g.model_dump(mode="json", exclude_none=True) for g in goals]},
        sort_keys=False,
        allow_unicode=True,
    )
