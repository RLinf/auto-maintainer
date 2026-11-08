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

"""Merging a PR's base branch into it when they conflict, for a maintainer (design 5.5).

A maintainer asks from the PR page (``dashboard.command`` with ``command:
merge_base``). The playbook starts the merge in a checkout of the PR's head; an agent
resolves each conflict, keeping what both sides meant, and may ask the maintainer; it
never commits, pushes, or runs the code. The playbook proposes a ``pr.fix`` action with
``merge_ref`` drafted for that maintainer, who reads the resolution and pushes the merge
commit onto the PR's branch as themselves. A merge without conflicts does not come here:
the dashboard has GitHub update the branch as the maintainer (``pr.update_branch``).
"""

from __future__ import annotations

from pathlib import Path

from ..domain.actions import ActionKind, FixPrPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import short_sha
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import NO_CODE_RULE, pr_block, untrusted

RESOLVE = """You are resolving merge conflicts for {reviewer}, a maintainer, inside an automated maintainer service. The working directory is a checkout of PR #{number}'s head with `{base}` merged into it; the files listed below still have conflict markers.
- Resolve every conflict in those files, keeping what both sides meant: the PR's change and what landed on `{base}` since it branched. When they cannot both stand, keep `{base}`'s behavior and adapt the PR's change to it.
- Remove every conflict marker. Change nothing outside the conflicted files, and do not commit, push, or post.
{no_code}
- When a conflict cannot be resolved without a decision only {reviewer} can make, ask them with your tool for asking the user.
- End with a short message in {public_language}: how you resolved each file, and what a test run on a test machine should check."""

MARKERS = ("<<<<<<< ", ">>>>>>> ")
"""The lines git puts around a conflict; a resolved file has none left."""


def only_files(patch: str, files: set[str]) -> str:
    """The part of ``patch`` that changes ``files``: what a maintainer reads of a
    merge is its resolution, not everything the base branch brings."""
    blocks = patch.split("diff --git ")
    if len(blocks) == 1:
        return patch
    kept = [
        "diff --git " + b
        for b in blocks[1:]
        if b.split("\n", 1)[0].split(" b/", 1)[-1].strip() in files
    ]
    return "".join(kept)


def unresolved(text: str) -> bool:
    """Whether ``text`` still has a conflict marker at the start of a line."""
    return any(line.startswith(MARKERS) for line in text.splitlines())


class MergeBasePlaybook(Playbook):
    name = "merge_base"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: object) -> bool:
        return event.payload.get("command") == "merge_base"

    def key(self, event: Event) -> str:
        return f"merge_base:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        by = str(event.payload["by"])
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        ctx.trusted_input = await ctx.author_is_team(pr)
        info = await ctx.forge.get_repo(pr.repo)
        base = pr.base_ref or info.default_branch
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            attempt = await ctx.workspace.merge(workdir, base)
            if not attempt.conflicts:
                return PlaybookResult(
                    result={
                        "skipped": f"{base} merges into the PR without conflicts; "
                        "update the branch instead"
                    }
                )
            listing = "\n".join(f"- {name}" for name in attempt.conflicts)
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt="\n\n".join(
                        [
                            RESOLVE.format(
                                reviewer=by,
                                number=pr.number,
                                base=base,
                                no_code=NO_CODE_RULE,
                                public_language=profile.languages.public,
                            ),
                            "Files with conflicts:\n"
                            + untrusted("merge.conflicts", listing, 8_000),
                            pr_block(pr),
                        ]
                    ),
                    workdir=workdir,
                    mode="workspace_write",
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    timeout_seconds=2400,
                )
            )
            if not answer.ok:
                if may_succeed_later(answer):
                    raise RuntimeError(f"the merge agent failed: {answer.error}")
                return PlaybookResult(failed=f"the merge agent failed: {answer.error}")
            resolved: dict[str, str] = {}
            left: list[str] = []
            for name in attempt.conflicts:
                path = Path(workdir) / name
                text = path.read_text(encoding="utf-8", errors="replace")
                if unresolved(text):
                    left.append(name)
                resolved[name] = text
            shown = only_files(
                (await ctx.workspace.changes(workdir)).patch, set(resolved)
            )
        finally:
            await ctx.workspace.release(workdir)
        if left:
            return PlaybookResult(
                failed="the agent left conflicts in " + ", ".join(left),
                result={"left": left, "summary": answer.text.strip()[-1000:]},
            )
        account = answer.text.strip()[-3000:] or f"Resolves the conflicts with {base}."
        action = make_action(
            ActionKind.PR_FIX,
            pr.repo,
            FixPrPayload(
                title=f"Merge {base} into #{pr.number}",
                body=account,
                patch=shown,
                base_sha=pr.head_sha,
                files=sorted(resolved),
                target="branch",
                merge_ref=base,
                merge_sha=attempt.merge_sha,
                resolved=resolved,
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=f"merge:{event.id}",
            rationale=f"merge {base} at {short_sha(attempt.merge_sha)} into "
            f"{short_sha(pr.head_sha)}, resolving {len(resolved)} conflicted files",
            draft_for=by,
        )
        return PlaybookResult(
            actions=[action],
            result={"resolved": sorted(resolved), "merge_sha": attempt.merge_sha},
        )
