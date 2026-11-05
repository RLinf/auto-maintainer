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

"""The profile doctor (design 5.2): check a profile against the live repository."""

from __future__ import annotations

import shlex

import yaml
from pydantic import BaseModel

from ..domain.codeowners import CODEOWNERS_LOCATIONS, parse_codeowners
from ..domain.ports import ForgeReader
from ..domain.profile import RepoProfile


class DoctorItem(BaseModel):
    check: str
    ok: bool
    detail: str = ""


async def workflow_names(forge: ForgeReader, repo: str) -> set[str]:
    names = set()
    for entry in await forge.list_dir(repo, ".github/workflows"):
        text = await forge.get_file(repo, f".github/workflows/{entry}")
        try:
            data = yaml.safe_load(text or "") or {}
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and data.get("name"):
            names.add(str(data["name"]))
    return names


async def run_doctor(
    profile: RepoProfile, forge: ForgeReader, lab_hosts: set[str]
) -> list[DoctorItem]:
    repo = profile.repo
    items: list[DoctorItem] = []
    info = await forge.get_repo(repo)
    labels = set(info.labels)
    for role, label in sorted(profile.labels.roles.items()):
        items.append(
            DoctorItem(
                check=f"label role {role}",
                ok=label in labels,
                detail=f"{label!r} {'exists' if label in labels else 'is missing from the repository'}",
            )
        )
    if profile.ci.required_workflows:
        names = await workflow_names(forge, repo)
        for wf in profile.ci.required_workflows:
            items.append(
                DoctorItem(
                    check=f"workflow {wf}",
                    ok=wf in names,
                    detail="found"
                    if wf in names
                    else f"no workflow named {wf!r}; found {sorted(names)}",
                )
            )
    skill = profile.review.skill
    if skill.startswith("repo:"):
        path = skill.split(":", 1)[1].split("#", 1)[0]
        found = await forge.get_file(repo, path) is not None
        items.append(
            DoctorItem(
                check="review skill",
                ok=found,
                detail=path if found else f"{path} is not on the default branch",
            )
        )
    for spec in profile.checks:
        if spec.type == "command" and spec.run:
            script = next(
                (
                    t
                    for t in shlex.split(spec.run)
                    if "/" in t and not t.startswith("{")
                ),
                None,
            )
            if script:
                found = await forge.get_file(repo, script) is not None
                items.append(
                    DoctorItem(
                        check=f"check {spec.id}",
                        ok=found,
                        detail=script
                        if found
                        else f"{script} is not on the default branch",
                    )
                )
        if spec.type == "builtin" and (spec.name or spec.id) == "paths_filter_coverage":
            wf = spec.settings.get("workflow", "")
            found = bool(wf) and await forge.get_file(repo, wf) is not None
            items.append(
                DoctorItem(
                    check=f"check {spec.id}",
                    ok=found,
                    detail=wf
                    if found
                    else f"workflow file {wf!r} not found; set checks[].settings.workflow",
                )
            )
    for lab, binding in profile.labs.items():
        if binding.use:
            ok = lab in lab_hosts
            items.append(
                DoctorItem(
                    check=f"lab {lab}",
                    ok=ok,
                    detail="declared by the operators"
                    if ok
                    else "no such lab host in local.yaml",
                )
            )
            if ok and not (binding.base_clone and binding.worktree_root):
                items.append(
                    DoctorItem(
                        check=f"lab {lab} paths",
                        ok=False,
                        detail="base_clone and worktree_root are needed (host overrides)",
                    )
                )
    locations = (
        [profile.people.codeowners]
        if profile.people.codeowners
        else list(CODEOWNERS_LOCATIONS)
    )
    text = None
    for loc in locations:
        text = await forge.get_file(repo, loc)
        if text:
            break
    if text is not None:
        rules = parse_codeowners(text)
        items.append(
            DoctorItem(check="CODEOWNERS", ok=bool(rules), detail=f"{len(rules)} rules")
        )
    if (
        profile.merge.enabled
        and profile.label_for("ci_gate") is None
        and not profile.ci.required_workflows
        and not info.required_checks
    ):
        items.append(
            DoctorItem(
                check="merge gate",
                ok=False,
                detail="merging is enabled but no checks are required",
            )
        )
    return items
