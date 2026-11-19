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

"""A fix for a review's findings (a follow-up's: its new ones and the earlier ones it
finds not fixed), or a change someone asked for in their own words: as
a PR into an open PR's branch (or straight onto it), or as a new PR or draft PR against
the repository's default branch when there is no open PR yet (design 5.5).

A maintainer asks for it from a review (``dashboard.command`` with ``command: fix``),
or in their own words (``request``): on the PR page, or in a conversation
(``thread_id``), which is told when the diff is ready.
An agent changes a checkout of the PR's head (or the default branch) in the
repository's folder, following the repository's own skills, and may ask the maintainer;
it never commits, pushes, or posts. The playbook proposes the diff as a ``pr.fix``
action drafted for that maintainer, who reads it and opens the PR as themselves: the
service's own account never pushes to a repository it maintains.
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, Field, ValidationError

from ..domain.actions import (
    Action,
    ActionKind,
    FixPrPayload,
    PrEditPayload,
    Provenance,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import short_sha
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, ChatMessage
from ..domain.review import Finding, FollowupResult, ReviewResult
from ..domain.snapshot import PrSnapshot, PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .ci_doctor import OUTSIDE_LOGS, outside_logs
from .prompts import (
    forge_note,
    language_line,
    schema_hint,
    skills_note,
    untrusted,
    work_lessons_block,
)

FIX = """Fix PR #{number} in {repo} for {reviewer}, a maintainer: {task}. The working directory is a checkout of its head `{head}`, with its base branch at `origin/{base}`.
- Make the smallest change that does it, keeping the PR author's approach, and follow the repository's own skills{skills} and conventions for what you change.
- Do not commit, push, or post: {reviewer} reads your diff and decides whether to open a PR with it. Run the repository's code only on its test machines, never here; its linters may run.
- When {reviewer} also wants the PR's title or description changed, give the new ones in your final message instead of writing them in a file; when that is all they want, change no file.
- Ask {reviewer}, with your tool for asking the user, only when something blocks a sound fix.
- End with what you changed{each}, what you left and why, and what a test run should cover.{language}"""

DESCRIBE = """Describe changes that an agent made to a pull request, for the PR that proposes them. Read the diff and what it was asked; do not change any file. Write in {public_language}: plain and specific.
Only when what was asked includes changing the pull request's own title or description, give the new ones in `pr_title` and `pr_body` (the whole description, starting from the current one below); otherwise leave both out."""

NEW_PR = """Prepare a {kind} against `{base_ref}` of `{repo}` for {reviewer}, a maintainer: do what they ask below. The working directory is a checkout of `{base_ref}` at `{sha}`.
- Make the smallest sound change that does it, and follow the repository's own skills{skills}, structure, and conventions.
- Do not commit, push, or open a PR: {reviewer} reads your diff and decides whether to open the {kind} as themselves. Run the repository's code only on its test machines, never here; its linters may run.
- Ask {reviewer}, with your tool for asking the user, only when something blocks a sound change.
- End with what you changed, what you left and why, and what a test run should cover.{language}"""

DESCRIBE_NEW_PR = """Describe changes an agent made on `{base_ref}` of `{repo}`, for the {kind} that opens them. Read the diff and what it was asked; do not change any file. Write `title` (in the repository's commit style) and `summary` (what the PR does and why) in {public_language}: plain and specific. Leave `pr_title` and `pr_body` unset."""


PR_TEXT = """Read what a maintainer asked an agent to do to a pull request, and the agent's answer. Only when what was asked includes changing the pull request's own title or description, give the new ones in `pr_title` and `pr_body`: as the agent's answer gives them, else written from the request, in {public_language}; `pr_body` is the whole description, starting from the current one below. Leave out either one that should stay as it is, and both when nothing about them was asked. Judge only from what is given here: do not run commands, read files, or fetch anything."""


class PrText(BaseModel):
    """A new title or description for the pull request itself, when one was asked for."""

    pr_title: str | None = Field(
        default=None, description="The new title, only when asked for."
    )
    pr_body: str | None = Field(
        default=None, description="The whole new description, only when asked for."
    )

    def pr_edit(self) -> dict[str, str]:
        """``pr_title`` and ``pr_body``, those set and not blank."""
        out = {}
        if self.pr_title and self.pr_title.strip():
            out["pr_title"] = self.pr_title.strip()[:256]
        if self.pr_body and self.pr_body.strip():
            out["pr_body"] = self.pr_body.strip()[:65_536]
        return out


def pr_text_task(
    public_language: str,
    pr: PrSnapshot,
    asked: list[str],
    account: str,
    workdir: object,
    requested_by: str | None = None,
) -> AgentTask:
    """The short task that reads whether an agent's change was asked to give the PR a
    new title or description (``PrText``), for a change that touched no file."""
    return AgentTask(
        kind=TaskKind.FIX,
        prompt="\n\n".join(
            [
                PR_TEXT.format(public_language=public_language),
                *asked,
                current_pr_text(pr),
                untrusted("agent.account", account[-4000:], 4000)
                if account
                else "The agent gave no answer.",
                schema_hint(PrText.model_json_schema()),
            ]
        ),
        workdir=workdir,
        mode="read_only",
        budget="quick",
        repo=pr.repo,
        pr=pr.number,
        requested_by=requested_by,
        untrusted_input=True,
        output_schema=PrText.model_json_schema(),
    )


def pr_edit_action(
    pr: PrSnapshot,
    edit: dict[str, str],
    by: str,
    *,
    discriminator: str,
    rationale: str,
    job_id: str | None = None,
) -> Action:
    """A ``pr.edit`` drafted for ``by``: the PR's new title and description from
    ``edit`` (``pr_title``, ``pr_body``), the current ones where it has none. Only
    ``by`` or a maintainer approves it, and it runs only as whoever approves it."""
    return make_action(
        ActionKind.PR_EDIT,
        pr.repo,
        PrEditPayload(
            title=edit.get("pr_title") or pr.title,
            body=edit.get("pr_body", pr.body or ""),
        ),
        number=pr.number,
        head_sha=pr.head_sha,
        discriminator=discriminator,
        rationale=rationale,
        draft_for=by,
        provenance=Provenance(job_id=job_id, playbook="fix", requested_by=by)
        if job_id
        else Provenance(requested_by=by),
    )


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
            and bool(event.repo)
            and bool(event.payload.get("by"))
            and (
                event.number is not None
                or bool(str(event.payload.get("request") or "").strip())
            )
        )

    def key(self, event: Event) -> str:
        target = (
            f"{event.repo}#{event.number}" if event.number is not None else event.repo
        )
        return f"fix:{target}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        if event.number is None:
            return await self._run_new_pr(ctx, event)
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
            if review.action.payload.get("kind") == "followup":
                listed = await followup_findings(ctx, review)
            else:
                listed = ReviewResult.model_validate(
                    review.action.payload.get("result") or {}
                ).findings
            wanted = set(event.payload.get("findings") or [])
            findings = [f for f in listed if not wanted or f.id in wanted]
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
            # Asked in their own words, a person may mean a failed check whose log the
            # agent's tools cannot reach (Read the Docs).
            outside = await outside_logs(ctx, pr, profile) if request or note else ""
            prompt = "\n\n".join(
                part
                for part in [
                    FIX.format(
                        number=pr.number,
                        repo=pr.repo,
                        reviewer=by,
                        head=short_sha(pr.head_sha),
                        base=info.default_branch,
                        skills=skills_note(workdir),
                        language=_language(profile),
                        task=f"do what {by} asks below"
                        if request
                        else "address the review findings below",
                        each="" if request else " for each finding",
                    ),
                    work_lessons_block(await ctx.knowledge.agent_lessons(pr.repo)),
                    asked,
                    f"What {by} added: " + untrusted("maintainer.note", note, 2000)
                    if note
                    else "",
                    OUTSIDE_LOGS + untrusted("ci.outside_logs", outside, 40_000)
                    if outside
                    else "",
                    forge_note(pr),
                ]
                if part
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
            await ctx.keep_work_session(answer, workdir)
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
            edit: dict[str, str] = {}
            if not changes.files:
                # Asked only for the PR's own title or description: no file changes.
                text = await ctx.ask(
                    pr_text_task(
                        profile.languages.public,
                        pr,
                        [asked],
                        account,
                        workdir,
                        requested_by=by,
                    ),
                    PrText,
                )
                if text.ok and text.structured is not None:
                    edit = PrText.model_validate(text.structured).pr_edit()
        finally:
            await ctx.workspace.release(workdir)
        if not changes.files and edit:
            await _tell(
                ctx,
                thread,
                f"The new {_what(edit)} for #{pr.number} are ready. Read them on the "
                "PR page or under Approvals, and approve them to set them as "
                "yourself; nothing changes before you do.",
            )
            return PlaybookResult(
                actions=[
                    pr_edit_action(
                        pr,
                        edit,
                        by,
                        discriminator=f"req:{event.id}",
                        rationale=f"a new {_what(edit)} for #{pr.number}, as {by} asked",
                    )
                ],
                result={"pr_edit": sorted(edit)},
            )
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
                draft=bool(event.payload.get("draft")),
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

    async def _run_new_pr(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        """Edit a checkout of ``event.repo``'s default branch from ``request`` and
        draft a ``pr.fix`` action that opens a new PR (or draft PR) as the approver."""
        profile = ctx.require_profile()
        assert event.repo
        by = str(event.payload["by"])
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        thread = event.payload.get("thread_id")
        draft = bool(event.payload.get("draft"))
        request = str(event.payload.get("request") or "").strip()
        note = str(event.payload.get("note") or "").strip()
        ctx.trusted_input = by in profile.people.team or (
            await ctx.permission(by)
        ).at_least(profile.people.team_min_permission)

        info = await ctx.forge.get_repo(event.repo)
        base_sha = info.default_branch_sha or await ctx.forge.default_branch_sha(
            event.repo
        )
        kind = "draft pull request" if draft else "pull request"
        workdir = await ctx.workspace.prepare(
            event.repo, None, base_sha, info.default_branch
        )
        try:
            asked = f"What {by} asked for:\n" + untrusted(
                "maintainer.request", request, 8_000
            )
            prompt = "\n\n".join(
                part
                for part in [
                    NEW_PR.format(
                        kind=kind,
                        base_ref=info.default_branch,
                        repo=event.repo,
                        sha=short_sha(base_sha),
                        reviewer=by,
                        skills=skills_note(workdir),
                        language=_language(profile),
                    ),
                    work_lessons_block(await ctx.knowledge.agent_lessons(event.repo)),
                    asked,
                    f"What {by} added: " + untrusted("maintainer.note", note, 2000)
                    if note
                    else "",
                ]
                if part
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt=prompt,
                    workdir=workdir,
                    mode="workspace_write",
                    skill_dirs=ctx.skill_dirs(),
                    repo=event.repo,
                    timeout_seconds=2400,
                )
            )
            if not answer.ok:
                if may_succeed_later(answer):
                    raise RuntimeError(f"fix agent failed: {answer.error}")
                await _tell(
                    ctx,
                    thread,
                    f"The agent could not prepare a PR for {event.repo}: {answer.error}",
                )
                return PlaybookResult(failed=f"fix agent failed: {answer.error}")
            account = answer.text.strip()
            await ctx.keep_work_session(answer, workdir)
            changes = await ctx.workspace.changes(workdir)
            described = None
            if changes.files:
                described = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.FIX,
                        prompt="\n\n".join(
                            [
                                DESCRIBE_NEW_PR.format(
                                    base_ref=info.default_branch,
                                    repo=event.repo,
                                    kind=kind,
                                    public_language=profile.languages.public,
                                ),
                                asked,
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
                        repo=event.repo,
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
                f"The agent changed nothing in {event.repo}."
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
        else:
            fix = FixResult(
                title=f"Changes to {event.repo}",
                summary=account[-2000:] or "The changes asked for.",
            )
        lines = [fix.summary]
        if fix.fixed:
            lines += ["", *[f"- {f.id}: {f.change}" for f in fix.fixed]]
        if fix.skipped:
            lines += [
                "",
                "Not changed:",
                *[f"- {f.id}: {f.reason}" for f in fix.skipped],
            ]
        lines += [
            "",
            f"Requested by {by} on `{info.default_branch}` at {short_sha(base_sha)}.",
        ]
        what = "draft PR" if draft else "PR"
        action = make_action(
            ActionKind.PR_FIX,
            event.repo,
            FixPrPayload(
                title=fix.title.strip()[:120] or f"Changes to {event.repo}",
                body="\n".join(lines).strip(),
                patch=changes.patch,
                base_sha=base_sha,
                base_ref=info.default_branch,
                draft=draft,
                files=changes.files,
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=None,
            head_sha=base_sha,
            discriminator=f"req:{event.id}",
            rationale=f"new {what} on {event.repo} ({info.default_branch}) {by} asked for",
            draft_for=by,
        )
        await _tell(
            ctx,
            thread,
            f"The {what} for {event.repo} is ready ({len(changes.files)} files: "
            f"{', '.join(changes.files[:8])}). Read the diff under Approvals, and "
            f"approve it to open the {what} as yourself; nothing is pushed before "
            "you do.",
        )
        return PlaybookResult(
            actions=[action],
            result={
                "files": changes.files,
                "draft": draft,
                "base_ref": info.default_branch,
            },
        )


async def followup_findings(
    ctx: PlaybookContext, followup: ActionRecord
) -> list[Finding]:
    """What a follow-up leaves to fix: the earlier findings it marks open or partly
    addressed, then its new findings. A follow-up keeps only the id, status, and note
    of an earlier finding, so its text comes from the PR's earlier ``review.post``
    actions (the newest with that id); one found in none of them keeps the status's
    title and note."""
    result = FollowupResult.model_validate(followup.action.payload.get("result") or {})
    earlier: dict[str, Finding] = {}
    reviews = await ctx.store.list_actions(
        repo=followup.action.repo,
        number=followup.action.number,
        kinds=[ActionKind.REVIEW_POST],
        limit=500,
    )
    for record in sorted(reviews, key=lambda r: r.action.created_at):
        if (
            record.action.id == followup.action.id
            or record.action.created_at > followup.action.created_at
        ):
            continue
        prior = record.action.payload.get("result") or {}
        for raw in [*(prior.get("findings") or []), *(prior.get("new_findings") or [])]:
            with contextlib.suppress(ValidationError):
                finding = Finding.model_validate(raw)
                earlier[finding.id] = finding
    still: list[Finding] = []
    for status in result.statuses:
        if status.status == "resolved":
            continue
        finding = earlier.get(status.id) or Finding(
            id=status.id,
            severity="major",
            category="",
            title=status.title or status.id,
            body="",
        )
        state = "still open" if status.status == "open" else "partly addressed"
        note = f"The follow-up found it {state}. {status.note}".strip()
        still.append(
            finding.model_copy(update={"body": f"{finding.body}\n{note}".strip()})
        )
    return [*still, *result.new_findings]


def _language(profile: RepoProfile) -> str:
    """The public language, after the prompt's last rule; empty for English."""
    line = language_line(profile)
    return f"\n- {line}" if line else ""


def _what(edit: dict[str, str]) -> str:
    """``title``, ``description``, or both, for what ``edit`` changes."""
    return " and ".join(
        name
        for key, name in (("pr_title", "title"), ("pr_body", "description"))
        if key in edit
    )


async def _tell(ctx: PlaybookContext, thread_id: object, text: str) -> None:
    """Tell the conversation that asked for the change, if one did."""
    if thread_id:
        await ctx.store.add_message(
            ChatMessage(thread_id=str(thread_id), role="assistant", text=text)
        )
