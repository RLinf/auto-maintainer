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

"""Packs: reusable bundles of profile settings for one convention."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .layers import Layer, LayerKind, ProfileError, parse_yaml


@dataclass
class Pack:
    name: str
    description: str
    profile: dict[str, Any]
    extends: list[str] = field(default_factory=list)
    skills_dir: Path | None = None
    source: str = ""


class PackLibrary:
    """Built-in packs from the code repo plus a deployment's packs from the memory repo."""

    def __init__(self, packs: dict[str, Pack]) -> None:
        self._packs = packs

    @classmethod
    def from_dirs(cls, *dirs: Path, extra: dict[str, str] | None = None) -> PackLibrary:
        """Load ``<dir>/<name>/pack.yaml`` files; ``extra`` maps names to YAML text."""
        packs: dict[str, Pack] = {}
        for root in dirs:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/pack.yaml")):
                pack = _parse(path.read_text(encoding="utf-8"), str(path))
                skills = path.parent / "skills"
                pack.skills_dir = skills if skills.is_dir() else None
                packs[pack.name] = pack
        for name, text in (extra or {}).items():
            packs[name] = _parse(text, f"memory:packs/{name}/pack.yaml")
        return cls(packs)

    def names(self) -> list[str]:
        return sorted(self._packs)

    def get(self, name: str) -> Pack:
        try:
            return self._packs[name]
        except KeyError:
            raise ProfileError(
                f"unknown pack {name!r}; known packs: {self.names()}"
            ) from None

    def layers_for(self, names: list[str]) -> list[Layer]:
        """Pack layers for ``names`` in dependency order (a pack's own ``extends`` first)."""
        ordered: list[str] = []

        def visit(name: str, stack: tuple[str, ...]) -> None:
            if name in stack:
                raise ProfileError(f"pack cycle: {' -> '.join((*stack, name))}")
            if name in ordered:
                return
            for dep in self.get(name).extends:
                visit(dep, (*stack, name))
            ordered.append(name)

        for name in names:
            visit(name, ())
        return [
            Layer(LayerKind.PACK, f"pack:{n}", self.get(n).profile, self.get(n).source)
            for n in ordered
        ]

    def skill_dirs(self, names: list[str]) -> list[Path]:
        return [
            p.skills_dir for n in names if (p := self._packs.get(n)) and p.skills_dir
        ]


def _parse(text: str, source: str) -> Pack:
    data = parse_yaml(text, source)
    name = data.get("name")
    if not name:
        raise ProfileError(f"{source}: a pack needs a name")
    return Pack(
        name=name,
        description=data.get("description", ""),
        profile=data.get("profile") or {},
        extends=list(data.get("extends") or []),
        source=source,
    )
