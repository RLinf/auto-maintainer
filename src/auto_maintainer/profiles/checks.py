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

"""Built-in hygiene checks (design 2.4). Each is a pure function of a PR and its profile.

Command and agent checks run in the ``hygiene`` playbook; they need a sandbox or an
agent.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..domain.codeowners import owners_for, parse_codeowners
from ..domain.profile import CheckSpec, RepoProfile
from ..domain.snapshot import PrSnapshot


class CheckResult(BaseModel):
    id: str
    passed: bool
    blocking: bool = False
    summary: str = ""
    details: list[str] = Field(default_factory=list)
    skipped: bool = False


@dataclass
class CheckInputs:
    """Files a check may need, read from the default branch by the playbook."""

    files: dict[str, str | None] = field(default_factory=dict)
    head_files: dict[str, str | None] = field(default_factory=dict)
    """Added or changed files at the PR head, for checks on file contents."""


Check = Callable[[PrSnapshot, RepoProfile, CheckSpec, CheckInputs], CheckResult]


def _result(
    spec: CheckSpec, passed: bool, summary: str, details: list[str] | None = None
) -> CheckResult:
    return CheckResult(
        id=spec.id,
        passed=passed,
        blocking=spec.blocking,
        summary=summary,
        details=details or [],
    )


def title_rule(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, _: CheckInputs
) -> CheckResult:
    rule = profile.contribution.title_rule
    if rule is None:
        return _result(spec, True, "no title rule configured")
    problems = []
    title = pr.title
    if rule.pattern and not re.match(rule.pattern, title):
        problems.append(f"does not match `{rule.pattern}`")
    desc = title.split(": ", 1)[1] if ": " in title else title
    if rule.max_description and len(desc) > rule.max_description:
        problems.append(
            f"the description after `: ` is {len(desc)} characters; at most {rule.max_description}"
        )
    if rule.lowercase_start and desc[:1].isupper():
        problems.append("the description should start with a lowercase letter")
    if rule.ascii_only and not title.isascii():
        problems.append("the title should be plain ASCII")
    if rule.no_trailing_period and title.endswith("."):
        problems.append("the title should not end with a period")
    return _result(
        spec,
        not problems,
        "PR title" if not problems else "PR title needs changes",
        problems,
    )


def commit_convention(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, _: CheckInputs
) -> CheckResult:
    if profile.contribution.commit_convention != "conventional":
        return _result(spec, True, "no commit convention configured")
    types = "|".join(map(re.escape, profile.contribution.conventional_types))
    pattern = re.compile(rf"^({types})(\([\w./-]+\))?!?: \S")
    bad = [
        f"`{c.sha[:7]}` {c.subject}"
        for c in pr.commits
        if not c.subject.startswith(("Merge ", 'Revert "'))
        and not pattern.match(c.subject)
    ]
    return _result(
        spec,
        not bad,
        "commit subjects follow Conventional Commits"
        if not bad
        else "commit subjects need the `type(scope): description` form",
        bad,
    )


def signoff(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, _: CheckInputs
) -> CheckResult:
    if not profile.contribution.signoff:
        return _result(spec, True, "sign-off not required")
    missing = []
    for c in pr.commits:
        if c.subject.startswith("Merge "):
            continue
        if not any(k.lower() == "signed-off-by" for k, _ in c.trailers):
            missing.append(f"`{c.sha[:7]}` {c.subject}")
    summary = (
        "every commit is signed off"
        if not missing
        else "commits without a `Signed-off-by:` trailer (use `git commit -s`)"
    )
    return _result(spec, not missing, summary, missing)


def template_sections(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, _: CheckInputs
) -> CheckResult:
    missing = []
    body = pr.body or ""
    for section in profile.contribution.template_sections:
        m = re.search(
            rf"^#+\s*{re.escape(section)}\s*$(.*?)(?=^#+\s|\Z)",
            body,
            re.MULTILINE | re.DOTALL,
        )
        content = (
            re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL).strip() if m else ""
        )
        if not content:
            missing.append(section)
    summary = (
        "the PR description fills the template"
        if not missing
        else "the PR description leaves template sections empty"
    )
    return _result(spec, not missing, summary, [f"`{s}`" for s in missing])


def license_header(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, inputs: CheckInputs
) -> CheckResult:
    header = profile.contribution.license_header
    if header is None:
        return _result(spec, True, "no license header configured")
    pattern = re.compile(header.pattern)
    bad = []
    for f in pr.files:
        if f.change_type.upper() != "ADDED" or not any(
            fnmatch.fnmatch(f.path, g) or fnmatch.fnmatch("/" + f.path, "/" + g)
            for g in header.paths
        ):
            continue
        text = inputs.head_files.get(f.path)
        if text is not None and not pattern.search("\n".join(text.splitlines()[:5])):
            bad.append(f"`{f.path}`")
    return _result(
        spec,
        not bad,
        "new files carry the license header"
        if not bad
        else "new files without the license header",
        bad,
    )


def codeowners_coverage(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, inputs: CheckInputs
) -> CheckResult:
    owners_text = inputs.files.get("CODEOWNERS")
    if not owners_text:
        return CheckResult(
            id=spec.id, passed=True, skipped=True, summary="no CODEOWNERS file"
        )
    rules = parse_codeowners(owners_text)
    orphan = [f"`{p}`" for p in pr.paths if not owners_for(rules, p)]
    return _result(
        spec,
        True,
        "every changed file has an owner"
        if not orphan
        else "changed files without a code owner",
        orphan,
    )


def _paths_filters(workflow_text: str) -> list[str]:
    """Glob patterns from dorny/paths-filter steps in a workflow."""
    patterns: list[str] = []
    try:
        data = yaml.safe_load(workflow_text) or {}
    except yaml.YAMLError:
        return patterns
    for job in (data.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if "paths-filter" not in str(step.get("uses", "")):
                continue
            filters = (step.get("with") or {}).get("filters")
            parsed = yaml.safe_load(filters) if isinstance(filters, str) else filters
            for value in (parsed or {}).values():
                items = value if isinstance(value, list) else [value]
                patterns += [i for i in items if isinstance(i, str)]
    return patterns


def _glob(path: str, pattern: str) -> bool:
    regex = (
        re.escape(pattern)
        .replace(r"\*\*/", "(?:.*/)?")
        .replace(r"\*\*", ".*")
        .replace(r"\*", "[^/]*")
    )
    return re.fullmatch(regex, path) is not None


def paths_filter_coverage(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, inputs: CheckInputs
) -> CheckResult:
    workflow = spec.settings.get("workflow", "")
    text = inputs.files.get(workflow)
    if not text:
        return CheckResult(
            id=spec.id,
            passed=True,
            skipped=True,
            summary=f"workflow {workflow} not found",
        )
    patterns = _paths_filters(text)
    exts = tuple(spec.settings.get("extensions", [".py", ".yaml", ".yml"]))
    unmatched = [
        f"`{p}`"
        for p in pr.paths
        if p.endswith(exts) and not any(_glob(p, g) for g in patterns)
    ]
    summary = (
        "changed files match a CI path filter"
        if not unmatched
        else f"changed files match no path filter in `{workflow}`"
    )
    return _result(spec, not unmatched, summary, unmatched)


def paired_paths(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, _: CheckInputs
) -> CheckResult:
    problems = []
    for pair in spec.settings.get("pairs", []):
        a, b = (p.rstrip("/") + "/" for p in pair)
        in_a = any(p.startswith(a) for p in pr.paths)
        in_b = any(p.startswith(b) for p in pr.paths)
        if in_a != in_b:
            changed, other = (a, b) if in_a else (b, a)
            problems.append(f"`{changed}` changed without `{other}`")
    return _result(
        spec,
        not problems,
        "paired docs changed together"
        if not problems
        else "paired docs are out of sync",
        problems,
    )


BUILTIN: dict[str, Check] = {
    "title_rule": title_rule,
    "commit_convention": commit_convention,
    "signoff": signoff,
    "template_sections": template_sections,
    "license_header": license_header,
    "codeowners_coverage": codeowners_coverage,
    "paths_filter_coverage": paths_filter_coverage,
    "paired_paths": paired_paths,
}


def files_needed(spec: CheckSpec) -> list[str]:
    name = spec.name or spec.id
    if name == "paths_filter_coverage":
        return [spec.settings.get("workflow", "")]
    return []


def run_builtin(
    pr: PrSnapshot, profile: RepoProfile, spec: CheckSpec, inputs: CheckInputs
) -> CheckResult:
    name = spec.name or spec.id
    check = BUILTIN.get(name)
    if check is None:
        return CheckResult(
            id=spec.id,
            passed=True,
            skipped=True,
            summary=f"unknown built-in check {name!r}",
        )
    return check(pr, profile, spec, inputs)


def render_checklist(results: list[CheckResult]) -> str:
    lines = ["### Hygiene checks", ""]
    for r in results:
        if r.skipped:
            continue
        mark = "✅" if r.passed else ("❌" if r.blocking else "⚠️")
        lines.append(
            f"- {mark} {r.summary}"
            + (" (blocks merging)" if r.blocking and not r.passed else "")
        )
        lines += [f"  - {d}" for d in r.details[:20]]
    return "\n".join(lines)


def to_data(results: list[CheckResult]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in results]
