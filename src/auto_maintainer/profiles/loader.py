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

"""Assemble a repository's candidate profile from its layers."""

from __future__ import annotations

from typing import Any

from ..domain.memory import profile_path, repo_dir
from ..domain.ports import ForgeReader, MemoryStore
from .layers import (
    Layer,
    LayerKind,
    MergedProfile,
    ProfileError,
    build_profile,
    parse_yaml,
)
from .packs import PackLibrary

IN_REPO_PATH = ".github/auto-maintainer.yml"


class ProfileLoader:
    """Reads every layer of a profile and merges them into a candidate."""

    def __init__(
        self,
        forge: ForgeReader,
        memory: MemoryStore,
        packs: PackLibrary,
        defaults: dict[str, Any],
        host_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._forge = forge
        self._memory = memory
        self._packs = packs
        self._defaults = defaults
        self._host = host_overrides or {}

    @property
    def packs(self) -> PackLibrary:
        return self._packs

    async def in_repo_layer(self, repo: str) -> Layer | None:
        text = await self._forge.get_file(repo, IN_REPO_PATH)
        if text is None:
            return None
        return Layer(
            LayerKind.IN_REPO,
            "in_repo",
            parse_yaml(text, f"{repo}:{IN_REPO_PATH}"),
            IN_REPO_PATH,
        )

    async def memory_layer(
        self, repo: str, override: dict[str, Any] | None = None
    ) -> Layer | None:
        """The memory layer, with a file-referenced rubric inlined so its hash covers it."""
        if override is not None:
            data = override
        else:
            text = await self._memory.read_text(profile_path(repo))
            if text is None:
                return None
            data = parse_yaml(text, profile_path(repo))
        data = dict(data)
        priority = dict(data.get("priority") or {})
        rubric = priority.get("rubric")
        if isinstance(rubric, str):
            path = f"{repo_dir(repo)}/{rubric}"
            rubric_text = await self._memory.read_text(path)
            if rubric_text is None:
                raise ProfileError(
                    f"{profile_path(repo)}: rubric file {path} does not exist"
                )
            priority["rubric"] = parse_yaml(rubric_text, path)
            data["priority"] = priority
        return Layer(LayerKind.MEMORY, "memory", data, profile_path(repo))

    async def layers(
        self, repo: str, memory_override: dict[str, Any] | None = None
    ) -> list[Layer]:
        in_repo = await self.in_repo_layer(repo)
        memory = await self.memory_layer(repo, memory_override)
        extends: list[str] = []
        for layer in (in_repo, memory):
            for name in (layer.data.get("extends") or []) if layer else []:
                if name not in extends:
                    extends.append(name)
        layers = [
            Layer(
                LayerKind.DEFAULTS,
                "defaults",
                self._defaults,
                "config/defaults/profile.yaml",
            )
        ]
        layers += self._packs.layers_for(extends)
        layers += [layer for layer in (in_repo, memory) if layer]
        host = self._host.get(repo)
        if host:
            layers.append(Layer(LayerKind.HOST, "host", host, "local.yaml"))
        return layers

    async def candidate(
        self, repo: str, memory_override: dict[str, Any] | None = None
    ) -> MergedProfile:
        """Merge the current layers; raises ``ProfileError`` when they do not validate."""
        return build_profile(repo, await self.layers(repo, memory_override))
