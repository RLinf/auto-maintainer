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

"""A fix for a review's findings, or a change someone asked for in their own words, as
a PR into the PR's branch (design 5.5).

A maintainer asks for it from a review (``dashboard.command`` with ``command: fix``),
or in their own words (``request``): on the PR page, or in a conversation
(``thread_id``), which is told when the diff is ready.
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
from ..domain.records import ChatMessage
from ..domain.review import Finding, ReviewResult
from ..domain.snapshot import PrSnapshot, PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import pr_block, review_skill_block, schema_hint, untrusted

FIX = """You are fixing a pull request for {reviewer}, a maintainer, inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
- Change the code to {task} and nothing else. Keep the PR author's approach; make the smallest change that does it.
- Check what you changed with the repository's static linters when it has them. Never run its tests, builds, scripts, or the PR's code on this machine: a repository's code runs only on its test machines, so say which tests a run there should cover.
- Do not commit, push, or post, and never ask whether to: the service shows your changes to {reviewer}, who decides whether to open a PR with them.
- When {reviewer} also asks to change the PR's title or description, do not write them in any file: give the new ones in your final message, and the service proposes them with your changes.
- When something blocks a sound fix (the intended behavior is unclear, two fixes are equally plausible), ask {reviewer} with your tool for asking the user.
- Write any text in {public_language}: plain and specific.
- When you are done, end with a short message: what you changed{each}, what you left and why, the checks you ran, and what a test run should cover."""

DESCRIBE = """Describe changes that an agent made to a pull request, for the PR that proposes them. Read the diff and what it was asked; do not change any file. Write in {public_language}: plain and specific.
Only when what was asked includes changing the pull request's own title or description, give the new ones in `pr_title` and `pr_body` (the whole description, starting from the current one below); otherwise leave both out."""


def current_pr_text(pr: PrSnapshot) -> str:
    """The PR's title and description as it has them, for ``DESCRIBE``."""
    return (
        untrusted("pr.title", pr.title, 500)
        + "\n\n"
        + untrusted("pr.body", pr.body or "(empty)", 12_000)
    )


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
    pr_title: str | None = Field(
        default=None,
        description="A new title for the pull request itself, only when asked for.",
    )
    pr_body: str | None = Field(
        default=None,
        description="A new description for the pull request itself, only when asked "
        "for: the whole text.",
    )

    def pr_edit(self) -> dict[str, str]:
        """``FixPrPayload``'s ``pr_title`` and ``pr_body``, those set and not blank."""
        out = {}
        if self.pr_title and self.pr_title.strip():
            out["pr_title"] = self.pr_title.strip()[:256]
        if self.pr_body and self.pr_body.strip():
            out["pr_body"] = self.pr_body.strip()[:65_536]
        return out


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
        thread = event.payload.get("thread_id")
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            await _tell(
                ctx, thread, f"#{event.number} is not open, so I changed nothing."
            )
            return PlaybookResult(result={"skipped": "the PR is not open"})
        request = str(event.payload.get("request") or "").strip()
        findings: list[Finding] = []
        if not request:
            review = await ctx.store.get_action(str(event.payload.get("review") or ""))
            if (
                review is None
                or review.action.kind != ActionKind.REVIEW_POST
                or review.action.number != pr.number
            ):
                return PlaybookResult(result={"skipped": "no review of this PR to fix"})
            result = ReviewResult.model_validate(
                review.action.payload.get("result") or {}
            )
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
            asked = (
                f"What {by} asked for:\n"
                + untrusted("maintainer.request", request, 8_000)
                if request
                else "Review findings to fix:\n"
                + untrusted("review.findings", listing, 20_000)
            )
            note = str(event.payload.get("note") or "").strip()
            prompt = "\n\n".join(
                [
                    FIX.format(
                        reviewer=by,
                        public_language=profile.languages.public,
                        task=f"do what {by} asks below"
                        if request
                        else "address the review findings below",
                        each="" if request else " for each finding",
                    ),
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    asked,
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
                await _tell(
                    ctx,
                    thread,
                    f"The agent could not change #{pr.number}: {answer.error}",
                )
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
                                asked,
                                current_pr_text(pr),
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
            await _tell(
                ctx,
                thread,
                f"The agent changed nothing in #{pr.number}."
                + (f" {account[-1500:]}" if account else ""),
            )
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
                title=f"Changes to #{pr.number}"
                if request
                else f"Fix review findings on #{pr.number}",
                summary=account[-2000:]
                or (
                    "The changes asked for."
                    if request
                    else "Fixes for the review's findings."
                ),
            )
        lines = [fix.summary, ""]
        lines += [f"- {f.id}: {f.change}" for f in fix.fixed]
        if fix.skipped:
            lines += ["", "Not changed:"] + [
                f"- {f.id}: {f.reason}" for f in fix.skipped
            ]
        lines += [
            "",
            f"For #{pr.number}, as {by} asked at {short_sha(pr.head_sha)}."
            if request
            else f"For #{pr.number}, from its review at {short_sha(pr.head_sha)}.",
        ]
        action = make_action(
            ActionKind.PR_FIX,
            pr.repo,
            FixPrPayload(
                title=fix.title.strip()[:120]
                or (
                    f"Changes to #{pr.number}"
                    if request
                    else f"Fix review findings on #{pr.number}"
                ),
                body="\n".join(lines).strip(),
                patch=changes.patch,
                base_sha=pr.head_sha,
                files=changes.files,
                findings=[f.id for f in fix.fixed] or [f.id for f in findings],
                runtime=answer.runtime,
                requested_by=by,
                **fix.pr_edit(),
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=f"req:{event.id}",
            rationale=f"changes to #{pr.number} {by} asked for"
            if request
            else f"fixes for {len(fix.fixed)} review findings, asked by {by}",
            draft_for=by,
        )
        await _tell(
            ctx,
            thread,
            f"The changes to #{pr.number} are ready ({len(changes.files)} files: "
            f"{', '.join(changes.files[:8])}). Read the diff on the PR page or under "
            "Approvals, and approve it to push it as yourself; nothing is pushed "
            "before you do.",
        )
        return PlaybookResult(
            actions=[action],
            result={"files": changes.files, "fixed": [f.id for f in fix.fixed]},
        )


async def _tell(ctx: PlaybookContext, thread_id: object, text: str) -> None:
    """Tell the conversation that asked for the change, if one did."""
    if thread_id:
        await ctx.store.add_message(
            ChatMessage(thread_id=str(thread_id), role="assistant", text=text)
        )
