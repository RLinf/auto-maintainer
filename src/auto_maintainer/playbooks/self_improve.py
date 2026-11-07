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
that repository's ``AGENTS.md``, and adds tests. The playbook runs the test suite and
the linter itself, then proposes the change as a ``code.pr`` action; the actuator
pushes it to an ``am/self/...`` branch and opens the PR. Nothing reaches the service's
default branch or its running code until a maintainer of its repository approves the
merge and, separately, the deploy.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, CodePrPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.records import ChatMessage
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import schema_hint

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
    """Runs only when a maintainer of the service's repository approved the request
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
            tests = await ctx.sandbox.run(TESTS, cwd=path, timeout=1800)
            lint = await ctx.sandbox.run(LINT, cwd=path, timeout=300)
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
                + _check(
                    "tests",
                    " ".join(TESTS),
                    tests.exit_code,
                    tests.stdout + tests.stderr,
                )
                + "\n"
                + _check(
                    "lint", " ".join(LINT), lint.exit_code, lint.stdout + lint.stderr
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
            result={
                "branch": branch,
                "files": changes.files,
                "tests_ok": tests.exit_code == 0,
                "lint_ok": lint.exit_code == 0,
            },
        )


def _prompt(request: str, by: Any, context: str) -> str:
    parts = [
        "You are changing auto-maintainer's own code: this worktree is its repository. "
        "Read AGENTS.md first and follow it, then the sections of docs/design.md it points to "
        "for the modules you touch.",
        f"A reviewer{f' ({by})' if by else ''} asked for this in a conversation with the service:\n\n{request}",
    ]
    if context:
        parts.append(f"What they wrote earlier in that conversation:\n\n{context}")
    parts.append(
        "Implement it with the smallest change that fits the existing design, with tests, and "
        "update AGENTS.md and docs/design.md where the behavior they describe changes. Run "
        "`uv run ruff format .`, `uv run ruff check .`, and `uv run pytest -q` and fix what fails. "
        "Do not commit, push, or open a PR: the service does that, and a maintainer approves it. "
        "If the request cannot or should not be done, change nothing and say why. When you "
        "are done, end with a short message: what you changed, and the commands you ran "
        "with their results."
    )
    return "\n\n".join(parts)


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


def _check(name: str, command: str, code: int, output: str) -> str:
    mark = "passed" if code == 0 else f"failed (exit {code})"
    tail = output.strip()[-1500:]
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
