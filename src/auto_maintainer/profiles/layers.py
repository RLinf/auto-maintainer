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

"""Profile layers and how they merge (design 2.2 and 5.2).

Layers merge in order: service defaults, packs (``extends``), the in-repo file, the
memory profile, and host overrides. Maps merge key by key; lists replace, except
the additive keys below. Each layer kind may only set some keys, so that, for
example, a pack cannot change autonomy and host overrides cannot change behavior.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import yaml
from pydantic import ValidationError

from ..domain.profile import PROFILE_SCHEMA, RepoProfile


class LayerKind(StrEnum):
    DEFAULTS = "defaults"
    PACK = "pack"
    IN_REPO = "in_repo"
    MEMORY = "memory"
    HOST = "host"


class ProfileError(ValueError):
    """A layer or the merged profile is invalid."""


@dataclass
class Layer:
    kind: LayerKind
    name: str
    data: dict[str, Any]
    source: str = ""

    @property
    def hash(self) -> str:
        return content_hash(self.data)


@dataclass
class MergedProfile:
    profile: RepoProfile
    data: dict[str, Any]
    layer_hashes: dict[str, str]
    origins: dict[str, str] = field(default_factory=dict)
    """Dotted key to the name of the layer that last set it."""

    @property
    def hash(self) -> str:
        return content_hash(self.data)


def content_hash(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


_FORBIDDEN: dict[LayerKind, set[str]] = {
    LayerKind.DEFAULTS: set(),
    LayerKind.PACK: {
        "repo",
        "policy",
        "ceilings",
        "access",
        "priority",
        "confidentiality",
        "routing",
        "memory",
    },
    LayerKind.IN_REPO: {
        "repo",
        "policy",
        "priority",
        "confidentiality",
        "routing",
        "notify",
        "memory",
    },
    LayerKind.MEMORY: {"ceilings"},
}
_HOST_LAB_KEYS = {
    "base_clone",
    "worktree_root",
    "env_roots",
    "throwaway_env_root",
    "protected_paths",
}

# Lists that accumulate across layers instead of replacing; the value is the item
# key used to replace an earlier item with the same identity (None: plain union).
_ADDITIVE: dict[tuple[str, ...], str | None] = {
    ("checks",): "id",
    ("ci", "flake_signatures"): "id",
    ("comments", "legacy_markers"): None,
    ("labs", "*", "guard", "forbid"): None,
}


def check_layer(layer: Layer) -> None:
    """Raise ``ProfileError`` if ``layer`` sets keys its kind may not set."""
    if not isinstance(layer.data, dict):
        raise ProfileError(f"{layer.name}: a profile layer must be a mapping")
    if layer.kind == LayerKind.HOST:
        extra = set(layer.data) - {"labs"}
        if extra:
            raise ProfileError(
                f"{layer.name}: host overrides may only set lab paths, found {sorted(extra)}"
            )
        for lab, binding in (layer.data.get("labs") or {}).items():
            bad = set(binding or {}) - _HOST_LAB_KEYS
            if bad:
                raise ProfileError(
                    f"{layer.name}: host overrides for lab {lab!r} may only set {sorted(_HOST_LAB_KEYS)}, found {sorted(bad)}"
                )
        return
    bad = set(layer.data) & _FORBIDDEN[layer.kind]
    if bad:
        raise ProfileError(
            f"{layer.name}: a {layer.kind} layer may not set {sorted(bad)}"
        )


def _additive_rule(path: tuple[str, ...]) -> tuple[bool, str | None]:
    for pattern, key in _ADDITIVE.items():
        if len(pattern) == len(path) and all(
            p in ("*", q) for p, q in zip(pattern, path, strict=True)
        ):
            return True, key
    return False, None


def _merge(
    base: Any, top: Any, path: tuple[str, ...], origins: dict[str, str], layer: str
) -> Any:
    if isinstance(base, dict) and isinstance(top, dict):
        out = dict(base)
        for k, v in top.items():
            sub = (*path, str(k))
            out[k] = (
                _merge(base.get(k), v, sub, origins, layer)
                if k in base
                else copy.deepcopy(v)
            )
            if k not in base:
                origins[".".join(sub)] = layer
        return out
    origins[".".join(path)] = layer
    additive, item_key = _additive_rule(path)
    if additive and isinstance(base, list) and isinstance(top, list):
        if item_key is None:
            return [*base, *[v for v in top if v not in base]]
        merged = {(i.get(item_key) if isinstance(i, dict) else None): i for i in base}
        for item in top:
            ident = item.get(item_key) if isinstance(item, dict) else None
            merged[ident] = (
                {**merged.get(ident, {}), **item} if ident in merged else item
            )
        return list(merged.values())
    return copy.deepcopy(top)


def merge_layers(layers: list[Layer]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge layer data in order; return the data and the origin of each key."""
    data: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for layer in layers:
        check_layer(layer)
        data = _merge(data, layer.data, (), origins, layer.name)
    labs = data.get("labs") or {}
    if "*" in labs:
        wildcard = labs.pop("*")
        for name in list(labs):
            labs[name] = _merge(
                wildcard, labs[name] or {}, ("labs", name), {}, "wildcard"
            )
    return data, origins


def build_profile(repo: str, layers: list[Layer]) -> MergedProfile:
    """Merge and validate ``layers`` into the profile of ``repo``."""
    data, origins = merge_layers(layers)
    data["repo"] = repo
    data.setdefault("schema", PROFILE_SCHEMA)
    extends: list[str] = []
    for layer in layers:
        for name in layer.data.get("extends", []) or []:
            if name not in extends:
                extends.append(name)
    data["extends"] = extends
    try:
        profile = RepoProfile.model_validate(data)
    except ValidationError as e:
        raise ProfileError(_explain(e)) from e
    hashes = {layer.name: layer.hash for layer in layers}
    return MergedProfile(
        profile=profile, data=profile.dump(), layer_hashes=hashes, origins=origins
    )


def _explain(error: ValidationError) -> str:
    lines = []
    for item in error.errors()[:10]:
        where = ".".join(str(p) for p in item["loc"])
        lines.append(f"{where}: {item['msg']}")
    return "invalid profile: " + "; ".join(lines)


def parse_yaml(text: str | None, source: str) -> dict[str, Any]:
    """Parse a YAML mapping, raising ``ProfileError`` with ``source`` on failure."""
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProfileError(f"{source}: invalid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: expected a mapping at the top level")
    return data


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
