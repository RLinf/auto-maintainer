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


"""Which required checks a PR's CI will never report, read from the base branch's
GitHub Actions workflows (design 5.5, merge scheduling).

A branch rule that requires a check waits for it forever when the workflow that runs
it skips the PR: its ``pull_request`` trigger's ``branches`` or ``paths`` filters
leave the PR out. ``never_run`` says so from the workflow files, so the schedule
tells the maintainer at once instead of waiting for CI that will not come. It judges
only plain cases: a check named exactly as a job (its ``name``, else its id) of
workflows that run on pull requests, none of which would run for the PR.
"""

from __future__ import annotations

from typing import Any

import yaml

from .conflicts import glob_match

_PR_EVENTS = ("pull_request", "pull_request_target")


def _triggers(data: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key ``on`` as ``True``.
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(e): {} for e in on}
    return {str(k): (v or {}) for k, v in on.items()} if isinstance(on, dict) else {}


def _filtered(patterns: list[str], value: str) -> bool:
    """Whether ``value`` passes a filter list, GitHub's way: the last pattern that
    matches decides, and a ``!`` pattern excludes."""
    passed = False
    for raw in patterns:
        pattern = str(raw)
        negated = pattern.startswith("!")
        if glob_match(pattern[1:] if negated else pattern, value):
            passed = not negated
    return passed


def _runs_for(config: dict[str, Any], base: str, paths: list[str]) -> tuple[bool, str]:
    """Whether one ``pull_request`` trigger runs for a PR into ``base`` that changes
    ``paths``, and, when not, why."""
    branches = config.get("branches")
    if isinstance(branches, list) and not _filtered(branches, base):
        return False, f"it runs only for PRs into {', '.join(map(str, branches[:3]))}"
    ignored = config.get("branches-ignore")
    if isinstance(ignored, list) and _filtered(ignored, base):
        return False, f"it skips PRs into {base}"
    wanted = config.get("paths")
    if (
        isinstance(wanted, list)
        and paths
        and not any(_filtered(wanted, p) for p in paths)
    ):
        return False, (
            "it runs only for PRs that change "
            + ", ".join(str(p) for p in wanted[:3])
            + (" …" if len(wanted) > 3 else "")
        )
    skip = config.get("paths-ignore")
    if isinstance(skip, list) and paths and all(_filtered(skip, p) for p in paths):
        return False, "it skips PRs that change only " + ", ".join(
            str(p) for p in skip[:3]
        )
    return True, ""


def _job_names(data: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for job_id, job in (data.get("jobs") or {}).items():
        if not isinstance(job, dict) or "strategy" in job or "uses" in job:
            continue  # a matrix or a reusable workflow names its checks otherwise
        name = job.get("name")
        names.add(
            str(name) if isinstance(name, str) and "${{" not in name else str(job_id)
        )
    return names


def never_run(
    files: dict[str, str], checks: list[str], paths: list[str], base: str
) -> dict[str, str]:
    """For each of ``checks`` that no workflow in ``files`` (file name to text)
    would run for a PR into ``base`` changing ``paths``, though one runs it for other
    PRs: why. Checks the workflows do not name are left out (another app reports
    them, or a matrix names them)."""
    runs: set[str] = set()
    skipped: dict[str, str] = {}
    for name, text in files.items():
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        triggers = _triggers(data)
        jobs = _job_names(data)
        events = [e for e in _PR_EVENTS if e in triggers]
        if not events:
            continue
        verdicts = [
            _runs_for(triggers[e] if isinstance(triggers[e], dict) else {}, base, paths)
            for e in events
        ]
        if any(ok for ok, _ in verdicts):
            runs |= jobs
            continue
        why = next(w for ok, w in verdicts if not ok)
        for job in jobs:
            skipped.setdefault(job, f"workflow {name}: {why}")
    return {c: skipped[c] for c in checks if c in skipped and c not in runs}
