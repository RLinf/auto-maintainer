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

"""Import rules between layers (design 4.4 and 4.9) and repository agnosticism (4.1)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parents[1] / "src" / "auto_maintainer"
ROOT = SRC.parents[1]

ADAPTERS = {
    "store",
    "github",
    "agents",
    "memory",
    "labs",
    "notify",
    "workspace",
    "selfupdate",
}
ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    **{a: {"domain", "subproc"} for a in ADAPTERS},
    "policy": {"domain"},
    "profiles": {"domain"},
    "playbooks": {"domain", "policy", "profiles", "playbooks"},
    "actuator": {"domain", "policy", "profiles"},
    "engine": {"domain", "policy", "profiles", "playbooks", "actuator"},
    "web": {
        "domain",
        "policy",
        "profiles",
        "playbooks",
        "actuator",
        "engine",
        "service",
    },
    "testing": {
        "domain",
        "policy",
        "profiles",
        "playbooks",
        "actuator",
        "engine",
        "service",
        "store",
        "resources",
    },
}
TOP_LEVEL_WIRING = {"app.py", "cli.py"}


def _imports(path: Path) -> set[str]:
    """First-level project packages that ``path`` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(SRC)
    depth = len(rel.parts) - 1
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            parts = list(rel.parts[: depth - node.level + 1]) + (
                node.module.split(".") if node.module else []
            )
            if parts:
                found.add(parts[0].removesuffix(".py"))
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("auto_maintainer.")
        ):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("auto_maintainer."):
                    found.add(alias.name.split(".")[1])
    return found


def test_layers() -> None:
    problems = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] in TOP_LEVEL_WIRING or "migrations" in rel.parts:
            continue
        package = rel.parts[0].removesuffix(".py") if len(rel.parts) > 1 else rel.stem
        if package not in ALLOWED:
            continue
        allowed = ALLOWED[package] | {package}
        for imported in _imports(path) - allowed - {"resources"}:
            problems.append(f"{rel} imports {imported}")
    assert not problems, "\n".join(sorted(problems))


def test_only_wiring_imports_adapters() -> None:
    for path in [SRC / "service.py"]:
        assert not (_imports(path) & ADAPTERS), f"{path.name} must not import adapters"


def test_only_the_leak_guard_uses_the_guard_key() -> None:
    users = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "GUARD_KEY" in p.read_text(encoding="utf-8")
    ]
    assert sorted(users) == ["domain/visibility.py", "policy/leak_guard.py"]


def test_playbooks_never_receive_a_writer() -> None:
    for path in (SRC / "playbooks").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ForgeWriter" not in text and "rerun_failed_jobs(" not in text, path.name


def _profile_names() -> set[str]:
    """Repository-specific names from the example and test profiles.

    Agent adapter tests are left out: their quoted strings are CLI protocol methods
    such as ``session/new`` and ``turn/start``, which have the shape of a repository
    name but belong in the adapters.
    """
    names: set[str] = set()
    sources = [
        p
        for p in ROOT.glob("tests/**/*.py")
        if "agents" not in p.relative_to(ROOT).parts
    ]
    for path in [*sources, *ROOT.glob("evals/**/*.yaml")]:
        for m in re.finditer(
            r"['\"]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]",
            path.read_text(encoding="utf-8"),
        ):
            if re.fullmatch(r"[a-z0-9-]+/[a-z0-9_.-]+", m.group(1)) and not m.group(
                1
            ).startswith(("src/", "tests/", "docs/", "ci/", ".")):
                names.add(m.group(1))
    return {n for n in names if "." not in n.split("/")[1] and n.count("/") == 1}


def test_core_names_no_target_repository() -> None:
    names = _profile_names() | {"RLinf", "rlinf", "a800"}
    allowed_files = {"testing/fakes.py", "testing/harness.py", "testing/demo.py"}
    hits = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in allowed_files:
            continue
        text = path.read_text(encoding="utf-8")
        body = "\n".join(text.splitlines()[13:])  # skip the license header
        for name in names:
            if re.search(rf"(?<![\w/.-]){re.escape(name)}(?![\w-])", body):
                hits.append(f"{rel}: {name}")
    assert not hits, "core code names repository-specific things:\n" + "\n".join(hits)


def test_default_profile_keeps_every_repository_action_in_shadow() -> None:
    data = yaml.safe_load(
        (ROOT / "config" / "defaults" / "profile.yaml").read_text(encoding="utf-8")
    )
    from auto_maintainer.domain.actions import ActionKind

    for kind in ActionKind:
        level = data["policy"][kind.value]["level"]
        if kind.touches_repository:
            assert level == "shadow", kind


def test_the_scanner_sees_imports() -> None:
    assert {"actuator", "playbooks"} <= _imports(SRC / "engine" / "engine.py")
    assert "acme/widget" in _profile_names()
