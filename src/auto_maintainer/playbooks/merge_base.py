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

A maintainer asks from the PR page or, for a self-improvement PR on ``code_repo``, from
the Service page (``dashboard.command`` with ``command: merge_base``). The playbook
starts the merge in a checkout of the PR's head; an agent resolves each conflict,
keeping what both sides meant, and may ask the maintainer; it never commits, pushes, or
runs the code. On a watched repository, the playbook proposes a ``pr.fix`` action with
``merge_ref`` drafted for that maintainer, who pushes the merge commit as themselves; on
``code_repo`` (``service_pr: true``), it proposes a ``code.update`` action for the owner
to approve and push onto the ``am/self/*`` branch. A merge without conflicts does not
come here: the dashboard updates the branch through the forge at once.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.actions import (
    ActionKind,
    CodeUpdatePayload,
    FixPrPayload,
    Provenance,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import short_sha
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import no_code_rule, pr_block, untrusted

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
    needs_profile = False

    def accepts(self, event: Event, profile: object) -> bool:
        if event.payload.get("command") != "merge_base":
            return False
        return profile is not None or bool(event.payload.get("service_pr"))

    def key(self, event: Event) -> str:
        return f"merge_base:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        service_pr = bool(event.payload.get("service_pr"))
        profile = None if service_pr else ctx.require_profile()
        assert event.repo and event.number
        if service_pr and event.repo != ctx.service.code_repo:
            return PlaybookResult(failed="not the service's code repository")
        by = str(event.payload["by"])
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if service_pr and (
            not pr.head_ref.startswith("am/self/")
            or pr.head_repo not in (None, "", pr.repo)
        ):
            return PlaybookResult(
                failed=f"{pr.repo}#{pr.number} is not a self-improvement PR"
            )
        if service_pr:
            ctx.trusted_input = True
            public_language = "en"
            default_branch = ctx.service.code_branch
        else:
            assert profile is not None
            ctx.trusted_input = await ctx.author_is_team(pr)
            public_language = profile.languages.public
            default_branch = (await ctx.forge.get_repo(pr.repo)).default_branch
        base = pr.base_ref or default_branch
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, default_branch
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
            if attempt.steering:
                return PlaybookResult(
                    failed=f"{base} conflicts with the PR in files that steer coding "
                    "agents, which no agent resolves from a PR's side: "
                    + ", ".join(attempt.steering)
                    + ". Resolve the merge by hand, or have the author do it.",
                    result={"steering": attempt.steering},
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
                                no_code=no_code_rule(pr.repo),
                                public_language=public_language,
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
        rationale = (
            f"merge {base} at {short_sha(attempt.merge_sha)} into "
            f"{short_sha(pr.head_sha)}, resolving {len(resolved)} conflicted files"
        )
        if service_pr:
            action = make_action(
                ActionKind.CODE_UPDATE,
                pr.repo,
                CodeUpdatePayload(
                    base_ref=base,
                    title=f"chore(self): merge {base} into #{pr.number}",
                    body=account,
                    patch=shown,
                    base_sha=pr.head_sha,
                    merge_ref=base,
                    merge_sha=attempt.merge_sha,
                    resolved=resolved,
                    files=sorted(resolved),
                    runtime=answer.runtime,
                    requested_by=by,
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"merge:{event.id}",
                rationale=rationale,
                draft_for=by,
                provenance=Provenance(requested_by=by),
            )
        else:
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
                rationale=rationale,
                draft_for=by,
            )
        return PlaybookResult(
            actions=[action],
            result={"resolved": sorted(resolved), "merge_sha": attempt.merge_sha},
        )
