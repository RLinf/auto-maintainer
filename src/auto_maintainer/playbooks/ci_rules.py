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


"""How a repository's CI starts and what its jobs mean, drafted by an agent for its
maintainers (design 5.5, merge scheduling).

On a maintainer's request from the Merge scheduling page, an agent reads the
default branch's workflows and contribution guide in a read-only checkout, beside
facts the service gathers (the branch rules, the fork-approval setting, and which
jobs fail at the open PRs' heads), and drafts ``ci.start`` and the plain-words
merge rules. The draft names only jobs that appear in a workflow file and a label
the repository has; it is kept for the page (``DRAFT_KEY``) and becomes the
profile's only when a maintainer saves it, through the configuration gate.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.run_ci import latest_checks
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import schema_hint

COMMAND = "ci_rules"
DRAFT_KEY = "ci-rules:{repo}"

INVESTIGATE = """You are finding out how the CI of {repo} starts and what it takes for a pull request to merge, for a service that runs the CI of the PRs its maintainers schedule and merges those that pass. The working directory is a read-only checkout of the default branch `{branch}`. Read `.github/workflows/` (the CI workflows, and the workflows they call) and the contribution guide (`CONTRIBUTING*`, `docs/` pages about contributing or CI), then answer:
- `how`: a few plain sentences a maintainer and an agent watching CI both need: what starts a PR's CI (pushes, a label, a maintainer approving a fork's workflows, a comment), which events start a run again (adding any label, a push), how runs replace or cancel each other (concurrency groups), and anything that keeps the full CI from running.
- `gate_label`: the label whose absence keeps the full CI from running, exactly as the workflows spell it; empty when there is none.
- `gate_jobs`: the job ids of jobs that fail on purpose when CI was not asked for (the gate label missing, a draft PR), as GitHub names the jobs.
- `summary_jobs`: the job ids of jobs that only collect the other jobs' results (typically `if: always()` with the others under `needs`).
- `start_minutes`: about how long after the starting event the runs begin; `run_minutes`: a generous bound on how long the full CI takes, from the workflows' timeouts and what the jobs do.
- `merge_notes`: what the contribution guide asks of a PR before it merges, beyond the forge's own rules listed below (for example who must approve); empty when it says nothing more.
- `approvals`: the number of approvals the guide asks for when it asks for more than the forge's rules do; otherwise 0.
Base every answer on what the files say; where they do not settle something, leave it empty and say so in `how`. Do not change files, run anything, or fetch anything."""


class CiRulesDraft(BaseModel):
    """What the agent found; checked before anyone sees it."""

    how: str = Field(description="How the CI starts, in plain sentences.")
    gate_label: str = Field(default="", description="The gate label, or empty.")
    gate_jobs: list[str] = Field(default_factory=list)
    summary_jobs: list[str] = Field(default_factory=list)
    start_minutes: int = Field(default=20, ge=1, le=24 * 60)
    run_minutes: int = Field(default=360, ge=5, le=7 * 24 * 60)
    merge_notes: str = Field(default="")
    approvals: int = Field(default=0, ge=0, le=20)


def _workflow_text(root: Path) -> str:
    folder = root / ".github" / "workflows"
    if not folder.is_dir():
        return ""
    parts = []
    for path in sorted(folder.iterdir()):
        if (
            path.suffix in (".yml", ".yaml")
            and path.is_file()
            and not path.is_symlink()
        ):
            parts.append(path.read_text(errors="replace"))
    return "\n".join(parts)


def known_jobs(text: str) -> set[str]:
    """The job ids and job names the workflow files declare."""
    ids = set(re.findall(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", text, re.MULTILINE))
    names = set(
        re.findall(r"^    name:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$", text, re.MULTILINE)
    )
    return ids | names


def checked(
    draft: CiRulesDraft, workflows: str, labels: list[str]
) -> tuple[CiRulesDraft, list[str]]:
    """``draft`` with what the repository does not have dropped, and what was."""
    jobs = known_jobs(workflows)
    dropped: list[str] = []
    keep = {}
    for field in ("gate_jobs", "summary_jobs"):
        names = []
        for name in getattr(draft, field):
            if name in jobs:
                names.append(name)
            else:
                dropped.append(f"job {name!r}, which no workflow declares")
        keep[field] = sorted(set(names))
    label = draft.gate_label.strip()
    if label and label not in labels:
        dropped.append(f"label {label!r}, which the repository does not have")
        label = ""
    return draft.model_copy(update={**keep, "gate_label": label}), dropped


class CiRulesPlaybook(Playbook):
    name = "ci_rules"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.payload.get("command") == COMMAND and bool(event.payload.get("by"))

    def key(self, event: Event) -> str:
        return f"ci_rules:{event.repo}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        ctx.require_profile()
        assert event.repo
        repo = event.repo
        by = str(event.payload.get("by"))
        ctx.requested_by = by
        ctx.trusted_input = True
        ctx.agent = event.payload.get("agent") or None
        info = await ctx.forge.get_repo(repo)
        failing: Counter[str] = Counter()
        prs = 0
        for pr in await ctx.store.list_snapshots(repo):
            prs += 1
            for c in latest_checks(pr.checks):
                if c.failed:
                    failing[c.name] += 1
        facts = "\n".join(
            [
                f"Default branch: {info.default_branch}",
                f"Required approvals: {info.required_approvals}",
                f"A code owner must approve: {info.requires_code_owner_reviews}",
                f"Conversations must be resolved: {info.requires_thread_resolution}",
                f"PRs must be up to date with the base: {info.requires_up_to_date}",
                f"Required checks: {', '.join(info.required_checks or []) or '(none)'}",
                f"Merge methods allowed: {', '.join(info.merge_methods or []) or 'unknown'}",
                f"Fork workflows wait for approval from: {info.fork_approval or 'unknown'}",
                f"Labels: {', '.join(info.labels[:200])}",
                f"Jobs failing at the heads of the {prs} open PRs: "
                + (
                    ", ".join(f"{n} ({k})" for n, k in failing.most_common(15))
                    or "(none)"
                ),
            ]
        )
        workdir = await ctx.workspace.prepare(
            repo,
            None,
            info.default_branch_sha or info.default_branch,
            info.default_branch,
        )
        try:
            prompt = "\n\n".join(
                [
                    INVESTIGATE.format(repo=repo, branch=info.default_branch),
                    "What the service read from the forge:\n" + facts,
                    schema_hint(CiRulesDraft.model_json_schema()),
                ]
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.ONBOARD,
                    prompt=prompt,
                    workdir=workdir,
                    mode="read_only",
                    output_schema=CiRulesDraft.model_json_schema(),
                    repo=repo,
                    requested_by=by,
                    activity="ci_rules",
                ),
                CiRulesDraft,
            )
            if not answer.ok or answer.structured is None:
                if may_succeed_later(answer):
                    return PlaybookResult(retry_after=300)
                return PlaybookResult(failed=answer.error or "no usable answer")
            draft, dropped = checked(
                CiRulesDraft.model_validate(answer.structured),
                _workflow_text(Path(workdir)),
                info.labels,
            )
        finally:
            await ctx.workspace.release(workdir)
        await ctx.store.set_state(
            DRAFT_KEY.format(repo=repo),
            {
                "draft": draft.model_dump(mode="json"),
                "dropped": dropped,
                "by": by,
                "at": ctx.clock.now().isoformat(),
                "sha": info.default_branch_sha,
            },
        )
        return PlaybookResult(
            result={"drafted": True, "dropped": dropped, "how": draft.how[:300]}
        )
