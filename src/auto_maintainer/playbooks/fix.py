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

"""A fix for a review's findings, as a PR into the reviewed PR's branch (design 5.5).

A maintainer asks for it from a review (``dashboard.command`` with ``command: fix``).
An agent changes a checkout of the PR's head in the repository's folder, following the
repository's own skills, and may ask the maintainer; it never commits, pushes, or
posts. The playbook proposes the diff as a ``pr.fix`` action drafted for that
maintainer, who reads it and opens the PR as themselves: the service's own account
never pushes to a repository it maintains.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, FixPrPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import short_sha
from ..domain.review import ReviewResult
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import pr_block, review_skill_block, schema_hint, untrusted

FIX = """You are fixing a pull request for {reviewer}, a maintainer, inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
- Change the code to address the review findings below and nothing else. Keep the PR author's approach; make the smallest change that resolves each finding.
- Run the repository's quick checks for what you changed when you can (its tests or linters).
- Do not commit, push, or post, and never ask whether to: the service shows your changes to {reviewer}, who decides whether to open a PR with them.
- When something blocks a sound fix (the intended behavior is unclear, two fixes are equally plausible), ask {reviewer} with your tool for asking the user.
- Write any text in {public_language}: plain and specific.
- When you are done, end with a short message: what you changed for each finding, what you left and why, and the checks you ran."""

DESCRIBE = """Describe fixes that an agent made to a pull request, for the PR that proposes them. Read the diff and the findings; do not change any file. Write in {public_language}: plain and specific."""


class FixedFinding(BaseModel):
    id: str
    change: str = Field(description="What you changed for this finding, in one line.")


class SkippedFinding(BaseModel):
    id: str
    reason: str


class FixResult(BaseModel):
    title: str = Field(
        description="A short PR title for the fixes, in the repository's commit style."
    )
    summary: str = Field(description="One paragraph on what the fixes do.")
    fixed: list[FixedFinding] = Field(default_factory=list)
    skipped: list[SkippedFinding] = Field(default_factory=list)


class FixPlaybook(Playbook):
    name = "fix"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event, profile) -> bool:  # type: ignore[no-untyped-def]
        return (
            event.payload.get("command") == "fix"
            and bool(event.payload.get("by"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        return f"fix:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        by = str(event.payload["by"])
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        review = await ctx.store.get_action(str(event.payload.get("review") or ""))
        if (
            review is None
            or review.action.kind != ActionKind.REVIEW_POST
            or review.action.number != pr.number
        ):
            return PlaybookResult(result={"skipped": "no review of this PR to fix"})
        result = ReviewResult.model_validate(review.action.payload.get("result") or {})
        wanted = set(event.payload.get("findings") or [])
        findings = [f for f in result.findings if not wanted or f.id in wanted]
        if not findings:
            return PlaybookResult(
                result={"skipped": "the review has no findings to fix"}
            )
        ctx.trusted_input = await ctx.author_is_team(pr)

        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            listing = "\n".join(
                f"- {f.id} [{f.severity}] {f.title} ({f.path}:{f.line}): {f.body}"
                + (f"\n  Suggested: {f.suggestion}" if f.suggestion else "")
                for f in findings
            )
            note = str(event.payload.get("note") or "").strip()
            prompt = "\n\n".join(
                [
                    FIX.format(reviewer=by, public_language=profile.languages.public),
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    "Review findings to fix:\n"
                    + untrusted("review.findings", listing, 20_000),
                    f"What {by} added: " + untrusted("maintainer.note", note, 2000)
                    if note
                    else "",
                    pr_block(pr, diff),
                ]
            )
            # The agent edits; the diff says what it did. Long edits often end
            # without a final answer in any fixed form, so a short second task
            # describes the diff for the PR.
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt=prompt,
                    workdir=workdir,
                    mode="workspace_write",
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    timeout_seconds=2400,
                )
            )
            if not answer.ok:
                if may_succeed_later(answer):
                    raise RuntimeError(f"fix agent failed: {answer.error}")
                return PlaybookResult(failed=f"fix agent failed: {answer.error}")
            account = answer.text.strip()
            changes = await ctx.workspace.changes(workdir)
            described = None
            if changes.files:
                described = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.FIX,
                        prompt="\n\n".join(
                            [
                                DESCRIBE.format(
                                    public_language=profile.languages.public
                                ),
                                "Review findings it was asked to fix:\n"
                                + untrusted("review.findings", listing, 20_000),
                                *(
                                    [untrusted("agent.account", account[-4000:], 4000)]
                                    if account
                                    else []
                                ),
                                untrusted("fix.diff", changes.patch, 60_000),
                                schema_hint(FixResult.model_json_schema()),
                            ]
                        ),
                        workdir=workdir,
                        mode="read_only",
                        repo=pr.repo,
                        untrusted_input=True,
                    ),
                    FixResult,
                )
        finally:
            await ctx.workspace.release(workdir)
        if not changes.files:
            return PlaybookResult(
                result={
                    "skipped": "the agent changed nothing",
                    "summary": account[-1000:],
                }
            )
        if described is not None and described.ok and described.structured is not None:
            fix = FixResult.model_validate(described.structured)
        else:  # the PR still names the findings and shows the diff
            fix = FixResult(
                title=f"Fix review findings on #{pr.number}",
                summary=account[-2000:] or "Fixes for the review's findings.",
            )
        lines = [fix.summary, ""]
        lines += [f"- {f.id}: {f.change}" for f in fix.fixed]
        if fix.skipped:
            lines += ["", "Not changed:"] + [
                f"- {f.id}: {f.reason}" for f in fix.skipped
            ]
        lines += ["", f"For #{pr.number}, from its review at {short_sha(pr.head_sha)}."]
        action = make_action(
            ActionKind.PR_FIX,
            pr.repo,
            FixPrPayload(
                title=fix.title.strip()[:120] or f"Fix review findings on #{pr.number}",
                body="\n".join(lines).strip(),
                patch=changes.patch,
                base_sha=pr.head_sha,
                files=changes.files,
                findings=[f.id for f in fix.fixed] or [f.id for f in findings],
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=f"req:{event.id}",
            rationale=f"fixes for {len(fix.fixed)} review findings, asked by {by}",
            draft_for=by,
        )
        return PlaybookResult(
            actions=[action],
            result={"files": changes.files, "fixed": [f.id for f in fix.fixed]},
        )
