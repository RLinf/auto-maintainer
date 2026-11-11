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

"""Self-improvement (design 5.12): a reviewer's request becomes a PR on the service's code.

A request comes from chat (``dashboard.command`` with ``command: improve``). An agent
changes a worktree of the service's own repository at its default branch, following
that repository's ``AGENTS.md``, and adds tests. The playbook runs the test suite,
the linter, and the format check itself; what fails goes back to the agent, at most
``MAX_REPAIRS`` times. Only a change that passes them all becomes a ``code.pr``
action; the actuator pushes it to an ``am/self/...`` branch and opens the PR. Nothing
reaches the service's default branch or its running code until a maintainer of its
repository approves the merge and, separately, the deploy.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, CodePrPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ports import CommandResult
from ..domain.profile import RepoProfile
from ..domain.records import ChatMessage
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import schema_hint, untrusted

TESTS = [
    "uv",
    "run",
    "--frozen",
    "pytest",
    "-q",
    "-x",
    "-m",
    "not live_agent and not live",
]
LINT = ["uv", "run", "--frozen", "ruff", "check", "."]
FORMAT = ["uv", "run", "--frozen", "ruff", "format", "--check", "."]
CHECKS = (("tests", TESTS, 1800), ("lint", LINT, 300), ("format", FORMAT, 300))
"""What a change must pass before it becomes a PR: name, command, timeout. The code
repository's CI (``.github/workflows/ci.yml``) runs the same."""

MAX_REPAIRS = 2
"""Turns the agent gets, after its first, to fix what the checks found."""

FAILURE_OUTPUT = 6_000
"""Characters of a failed check's output, from its end, that the agent reads."""


class ImproveSummary(BaseModel):
    """How the change reads on its PR, written from the diff after the work is done."""

    title: str = Field(
        description=(
            "PR title: `<type>(<scope>): <description>`, imperative; the description at most "
            "50 characters, lowercase start, no final period."
        )
    )
    summary: str = Field(
        description="What changed and why, for the PR description: the behavior, the files, the design sections touched."
    )
    notes: str = Field(
        default="",
        description="Anything a maintainer should check before approving: risks, follow-ups, open questions.",
    )


MAX_DIFF_FOR_SUMMARY = 60_000
"""Characters of the diff the summarizing agent reads."""


class SelfImprove(Playbook):
    """Runs only when the owner of this service approved the request
    (the chat's request card). The agent edits a worktree and does not need to answer
    in a fixed format: the diff says what it did, and a short second task describes
    the diff for the PR."""

    name = "self_improve"
    triggers: ClassVar = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"
    needs_profile = False

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.payload.get("command") == "improve" and bool(
            str(event.payload.get("request") or "").strip()
        )

    def key(self, event: Event) -> str:
        return f"self_improve:{event.payload.get('request_id') or event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        code_repo = ctx.service.code_repo
        thread_id = event.payload.get("thread_id")
        if not code_repo:
            await _tell(
                ctx,
                thread_id,
                "This service has no code repository configured (`code_repo` in local.yaml), so it cannot change its own code.",
            )
            return PlaybookResult(failed="no code repository is configured")
        request = str(event.payload["request"]).strip()
        by = event.payload.get("by")
        approved_by = event.payload.get("approved_by")
        # The request a maintainer of the code repository approved, the asker's own
        # messages, and the service's own code: nothing from outside the team, so a
        # bypass the agent's settings allow applies.
        ctx.trusted_input = True
        base = await ctx.forge.default_branch_sha(code_repo)
        path = await ctx.workspace.prepare(
            code_repo, None, base, ctx.service.code_branch
        )
        try:
            work = await ctx.ask(
                AgentTask(
                    kind=TaskKind.SELF_IMPROVE,
                    prompt=_prompt(request, by, event.payload.get("context") or ""),
                    workdir=path,
                    mode="workspace_write",
                    repo=code_repo,
                )
            )
            if not work.ok:
                if may_succeed_later(work):
                    await _tell(
                        ctx,
                        thread_id,
                        f"I could not work on the change yet ({work.error}); I will try again.",
                    )
                    raise RuntimeError(f"self-improvement agent failed: {work.error}")
                await _tell(
                    ctx, thread_id, f"I could not work on the change: {work.error}"
                )
                return PlaybookResult(failed=work.error or "the agent failed")
            account = work.text.strip()
            changes = await ctx.workspace.changes(path)
            if not changes.patch.strip():
                await _tell(
                    ctx,
                    thread_id,
                    "I did not change any code."
                    + (f" {account[-1500:]}" if account else ""),
                )
                return PlaybookResult(result={"done": False})
            checks = await _run_checks(ctx, path)
            repairs = 0
            while _failed(checks) and repairs < MAX_REPAIRS:
                repairs += 1
                fixed = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.SELF_IMPROVE,
                        prompt=_repair_prompt(request, checks),
                        workdir=path,
                        mode="workspace_write",
                        repo=code_repo,
                    ),
                    after=work,
                )
                if not fixed.ok:
                    break
                work = fixed
                account = fixed.text.strip() or account
                checks = await _run_checks(ctx, path)
            if _failed(checks):
                changes = await ctx.workspace.changes(path)
                failing = ", ".join(_failed(checks))
                tries = f"{repairs + 1} {'tries' if repairs else 'try'}"
                await _tell(
                    ctx,
                    thread_id,
                    f"I did not open a PR: after {tries}, these checks still fail: "
                    f"{failing}. Files I changed: "
                    f"{', '.join(changes.files[:40]) or 'none'}.\n\n"
                    + "\n".join(
                        _check(name, " ".join(argv), result)
                        for name, argv, result in checks
                        if result.exit_code != 0
                    ),
                )
                return PlaybookResult(
                    failed=f"checks still fail after {tries}: {failing}",
                    result={"files": changes.files, "failed": _failed(checks)},
                )
            changes = await ctx.workspace.changes(path)
            described = await ctx.ask(
                AgentTask(
                    kind=TaskKind.SELF_IMPROVE,
                    prompt=_summary_prompt(request, changes.patch, account),
                    workdir=path,
                    mode="read_only",
                    repo=code_repo,
                ),
                ImproveSummary,
            )
        finally:
            await ctx.workspace.release(path)

        if described.ok and described.structured is not None:
            summary = ImproveSummary.model_validate(described.structured)
        else:  # the PR still says what was asked and shows the diff
            summary = ImproveSummary(
                title=request.splitlines()[0],
                summary=account[-3000:] or "See the diff.",
            )
        title = _title(summary.title)
        branch = f"am/self/{_slug(title)}-{event.id[-6:]}"
        body = "\n\n".join(
            part
            for part in [
                (f"Requested by {by} in chat" if by else "Requested in chat")
                + (f", approved to start by {approved_by}" if approved_by else "")
                + ":",
                "> " + request.replace("\n", "\n> "),
                "## What changed\n" + summary.summary,
                "## Checks\n"
                + "\n".join(
                    _check(name, " ".join(argv), result)
                    for name, argv, result in checks
                ),
                "## The agent's account\n" + account[-3000:] if account else "",
                "## Notes for the reviewer\n" + summary.notes if summary.notes else "",
                f"Files: {', '.join(changes.files[:40])}",
                f"Drafted by auto-maintainer with {work.runtime}. A maintainer of this "
                "repository must approve the merge, and then the deploy, before the service "
                "runs this code.",
            ]
            if part
        )
        action = make_action(
            ActionKind.CODE_PR,
            code_repo,
            CodePrPayload(
                branch=branch,
                title=title,
                body=body,
                patch=changes.patch,
                base_sha=changes.base_sha,
                requested_by=by,
                thread_id=thread_id,
            ),
            discriminator=branch,
        )
        action.provenance.requested_by = by
        action.rationale = f"self-improvement requested by {by or 'a reviewer'}"
        return PlaybookResult(
            actions=[action],
            result={"branch": branch, "files": changes.files, "repairs": repairs},
        )


CLOSING = (
    "Do not commit, push, or open a PR: the service does that, and a maintainer approves "
    "it. Wait for every command you start to finish and read its result; do not end "
    "while one still runs. When you are done, end with a short message: what you "
    "changed, and the commands you ran with their results."
)


def _prompt(request: str, by: Any, context: str) -> str:
    parts = [
        "You are changing auto-maintainer's own code: this worktree is its repository. "
        "Read AGENTS.md first and follow it. docs/design.md is long: find the sections "
        "for the modules you touch (search its headings) and read those, not the whole "
        "file.",
        f"A reviewer{f' ({by})' if by else ''} asked for this in a conversation with the service:\n\n{request}",
    ]
    if context:
        parts.append(f"What they wrote earlier in that conversation:\n\n{context}")
    parts.append(
        "Implement what was asked, and no more, with the smallest change that fits the "
        "existing design, with tests, and update AGENTS.md and docs/design.md where the "
        "behavior they describe changes. Run `uv run ruff format .`, `uv run ruff check "
        ".`, and the tests of what you changed (`uv run pytest -q tests/<file>`), and fix "
        "what fails. After you finish, the service runs the whole suite, Ruff, and the "
        "format check, and sends you anything that fails. If the request cannot or "
        "should not be done, change nothing and say why. " + CLOSING
    )
    return "\n\n".join(parts)


def _repair_prompt(
    request: str, checks: list[tuple[str, list[str], CommandResult]]
) -> str:
    """The next turn of the editing agent, or a fresh agent in the same worktree:
    what was asked, and the end of each failing check's output."""
    failures = [
        f"`{' '.join(argv)}` exited with {result.exit_code}"
        + (" (timed out)" if result.timed_out else "")
        + ":\n"
        + untrusted(
            f"checks.{name}",
            (result.stdout + result.stderr).strip()[-FAILURE_OUTPUT:],
            FAILURE_OUTPUT,
        )
        for name, argv, result in checks
        if result.exit_code != 0
    ]
    return "\n\n".join(
        [
            "The service ran its checks on your change to auto-maintainer's own code, "
            "and some failed. The worktree still holds the change. Fix what they report, "
            "keeping to what was asked; if a check fails for a reason the change did not "
            "cause, say so.",
            f"What was asked:\n\n{request}",
            *failures,
            "Run what failed again (the failing tests, Ruff, or the format check) "
            "before you end. " + CLOSING,
        ]
    )


async def _run_checks(
    ctx: PlaybookContext, path: Any
) -> list[tuple[str, list[str], CommandResult]]:
    return [
        (
            name,
            argv,
            await ctx.sandbox.run(argv, cwd=path, timeout=timeout, untrusted=True),
        )
        for name, argv, timeout in CHECKS
    ]


_DIGITS = re.compile(r"\d+(\.\d+)?")


def _failures(checks: list[tuple[str, list[str], CommandResult]]) -> set[str]:
    """What the failing checks report, without their timings: the same set after a
    repair turn means the turn fixed nothing."""
    return {
        f"{name}: {_DIGITS.sub('#', line.strip())}"
        for name, _, result in checks
        if result.exit_code != 0
        for line in (result.stdout + result.stderr).splitlines()
        if line.startswith(("FAILED", "ERROR", "E ", "error", "Would reformat"))
        or (": " in line and line.split(":", 1)[0].endswith(".py"))
    }


def _failed(checks: list[tuple[str, list[str], CommandResult]]) -> list[str]:
    return [name for name, _, result in checks if result.exit_code != 0]


def _summary_prompt(request: str, patch: str, account: str) -> str:
    diff = (
        patch
        if len(patch) <= MAX_DIFF_FOR_SUMMARY
        else patch[:MAX_DIFF_FOR_SUMMARY] + "\n…"
    )
    return "\n\n".join(
        [
            "Describe this change to auto-maintainer's own code for its pull request. Read "
            "the diff; do not change any file.",
            f"What a reviewer asked for:\n\n{request}",
            *(
                [f"What the agent that made it said:\n\n{account[-4000:]}"]
                if account
                else []
            ),
            f"The diff:\n\n```diff\n{diff}\n```",
            schema_hint(ImproveSummary.model_json_schema()),
        ]
    )


def _title(title: str) -> str:
    """A PR title in the code repository's form: ``<type>(<scope>): <description>``, the
    description at most 50 ASCII characters, starting lowercase, without a final period."""
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    title = " ".join(ascii_text.split())
    m = re.match(r"^([a-z]+(?:\([^)]+\))?!?): (.*)$", title)
    prefix, description = (m.group(1), m.group(2)) if m else ("feat", title)
    description = description.rstrip(".").strip()[:50].rstrip(" .")
    description = (description[:1].lower() + description[1:]) or "change"
    return f"{prefix}: {description}"


def _slug(title: str) -> str:
    text = re.sub(r"^[a-z]+(\([^)]+\))?!?: ", "", title.lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "change"


def _check(name: str, command: str, result: CommandResult) -> str:
    code = result.exit_code
    mark = "passed" if code == 0 else f"failed (exit {code})"
    tail = (result.stdout + result.stderr).strip()[-1500:]
    return f"- {name}: `{command}` {mark}" + (
        f"\n\n<details><summary>output</summary>\n\n```\n{tail}\n```\n</details>"
        if tail
        else ""
    )


async def _tell(ctx: PlaybookContext, thread_id: Any, text: str) -> None:
    if thread_id:
        await ctx.store.add_message(
            ChatMessage(thread_id=str(thread_id), role="assistant", text=text)
        )
