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

"""Comment checks (design 5.6): whether a PR's open review comments are addressed
by its code now, for its review progress.

``CommentCheckPlaybook`` reads only what each comment needs: the comment thread, the
changes to its file since the comment was written, and the code around its line at
the PR's head; one quick agent run judges every comment of the PR at once and sums
each up in one line. It posts nothing: the results go to the store
(``domain/review_progress.checks_key``), where the PR's review progress counts a
comment found addressed as fixed. A check runs when someone asks on the dashboard
(``comments.check``, for some comments or all) or from ``CommentScan`` on the
repository's ``review.comments.schedule``; never on each push. A periodic check reads
again only the comments whose file changed since they were last checked, so a PR
that did not change costs nothing.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.review_progress import CommentCheck, checks_key, load_checks
from ..domain.scoring import priority_key
from ..domain.snapshot import PrSnapshot, PrState, ReviewThread
from .base import Playbook, PlaybookContext, PlaybookResult, in_scope
from .evaluate import rubric_of
from .prompts import schema_hint, untrusted
from .review_scan import scan_due

SCAN_STATE = "comment_scan:{repo}"
CONTEXT_LINES = 25
_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


class CheckedComment(BaseModel):
    id: str
    summary: str = Field(max_length=200)
    """What the comment asks, in one line."""
    status: Literal["addressed", "partly", "open", "unclear"]
    reason: str = Field(default="", max_length=400)
    fixed_in: str | None = None
    """The commit that addressed it, when the changes show which."""


class CommentCheckResult(BaseModel):
    items: list[CheckedComment] = Field(default_factory=list)


def open_threads(pr: PrSnapshot) -> list[ReviewThread]:
    """The PR's unresolved review threads someone other than its author opened."""
    author = pr.author.lower()
    return [
        t
        for t in pr.threads
        if not t.is_resolved and t.comments and t.comments[0].author.lower() != author
    ]


def file_sections(diff: str) -> dict[str, str]:
    """A unified diff split by the file each part changes (its new path)."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff.splitlines():
        m = _FILE.match(line)
        if m:
            current = out.setdefault(m.group(2), [])
        if current is not None:
            current.append(line)
    return {path: "\n".join(lines) for path, lines in out.items()}


def _around(workdir: Path, path: str | None, line: int | None) -> str:
    """The code around ``line`` of ``path`` at the checkout, numbered."""
    if not path:
        return ""
    target = (workdir / path).resolve()
    if not target.is_relative_to(workdir.resolve()) or not target.is_file():
        return "(the file is not at the head any more)"
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if line is None:
        return ""
    start = max(0, line - 1 - CONTEXT_LINES)
    end = min(len(lines), line + CONTEXT_LINES)
    return "\n".join(f"{i + 1:>5}  {lines[i]}" for i in range(start, end))


class CommentCheckPlaybook(Playbook):
    name = "comment_check"
    triggers = (EventKind.COMMENT_CHECK,)
    pool = "agent"

    def key(self, event: Event) -> str:
        return f"comment_check:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number is not None
        by = event.payload.get("by")
        if by:
            ctx.requested_by = str(by)
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        wanted = set(event.payload.get("thread_ids") or [])
        key = checks_key(pr.repo, pr.number)
        known = load_checks(await ctx.store.get_state(key))
        threads = [t for t in open_threads(pr) if not wanted or t.id in wanted]
        if not wanted:
            # Nobody named comments: read again only what changed since its check.
            threads = [
                t
                for t in threads
                if t.id not in known or known[t.id].head_sha != pr.head_sha
            ]
        batch = profile.review.comments.max_comments
        if not wanted:
            # The comments a person named are all checked, ``batch`` at a time; on
            # its own the check reads at most ``batch`` per run.
            threads = threads[:batch]
        if not threads:
            return PlaybookResult(result={"skipped": "no open comment to check"})

        info = await ctx.forge.get_repo(pr.repo)
        workdir = Path(
            await ctx.workspace.prepare(
                pr.repo, pr.number, pr.head_sha, info.default_branch
            )
        )
        checked = dict(known)
        found: list[CommentCheck] = []
        live = {t.id for t in pr.threads}
        try:
            diffs: dict[str, dict[str, str]] = {}

            async def sections(base: str) -> dict[str, str]:
                if base not in diffs:
                    diffs[base] = file_sections(
                        await ctx.workspace.diff(workdir, base, pr.head_sha)
                    )
                return diffs[base]

            if not wanted:
                # A comment checked before whose file did not change since keeps
                # its result.
                kept = []
                for t in threads:
                    old = known.get(t.id)
                    if old is None:
                        kept.append(t)
                        continue
                    changed = await sections(old.head_sha)
                    if t.path in changed:
                        kept.append(t)
                threads = kept
                if not threads:
                    return PlaybookResult(
                        result={"skipped": "no commented file changed since its check"}
                    )

            for start in range(0, len(threads), batch):
                part = threads[start : start + batch]
                ids = {f"c{i}": t for i, t in enumerate(part, 1)}
                answer = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.COMMENT_CHECK,
                        prompt=await _prompt(
                            ids, sections, workdir, info.default_branch, pr, profile
                        ),
                        workdir=workdir,
                        output_schema=CommentCheckResult.model_json_schema(),
                        repo=pr.repo,
                        pr=pr.number,
                        budget="quick",
                    ),
                    CommentCheckResult,
                )
                if not answer.ok or answer.structured is None:
                    raise RuntimeError(f"the comment check failed: {answer.error}")
                result = CommentCheckResult.model_validate(answer.structured)
                now: datetime = ctx.clock.now()
                for item in result.items:
                    thread = ids.get(item.id)
                    if thread is None:
                        continue
                    checked[thread.id] = CommentCheck(
                        thread_id=thread.id,
                        status=item.status,
                        summary=" ".join(item.summary.split())[:200],
                        reason=" ".join(item.reason.split())[:400],
                        fixed_in=item.fixed_in,
                        head_sha=pr.head_sha,
                        checked_at=now,
                    )
                    found.append(checked[thread.id])
                # Each batch shows on the dashboard as soon as it is done.
                await ctx.store.set_state(
                    key,
                    {
                        k: v.model_dump(mode="json")
                        for k, v in checked.items()
                        if k in live
                    },
                )
        finally:
            await ctx.workspace.release(workdir)
        return PlaybookResult(
            result={
                "checked": len(found),
                "addressed": sum(1 for c in found if c.status == "addressed"),
            }
        )


async def _prompt(
    ids: dict[str, ReviewThread],
    sections: Callable[[str], Awaitable[dict[str, str]]],
    workdir: Path,
    default_branch: str,
    pr: PrSnapshot,
    profile: RepoProfile,
) -> str:
    """What the agent reads to check the comments ``ids`` against the code now."""
    blocks = []
    for cid, t in ids.items():
        since = t.comments[0].commit_sha or f"origin/{default_branch}"
        change = (await sections(since)).get(t.path or "", "")
        talk = "\n\n".join(f"{c.author}: {c.body}" for c in t.comments)
        blocks.append(
            "\n".join(
                [
                    f"### Item {cid}: {t.path or '(no file)'}"
                    f"{f':{t.line}' if t.line else ' (lines changed since)'}",
                    untrusted(f"{cid}.comments", talk, 3000),
                    untrusted(
                        f"{cid}.changes_since_comment",
                        change or "(this file did not change since the comment)",
                        6000,
                    ),
                    untrusted(
                        f"{cid}.code_now",
                        _around(workdir, t.path, t.line) or "(not shown)",
                        4000,
                    ),
                ]
            )
        )
    return "\n\n".join(
        [
            "Reviewers left these comments on a pull request. For each item, say "
            "in one line (at most 100 characters) what the comment asks, then "
            "whether the code as it is now does it: `addressed` (it does, or the "
            "concern no longer applies), `partly`, `open` (not yet), or "
            "`unclear` (a question or discussion the code cannot settle, or "
            "what you were given does not show). Give a one-line reason, and "
            "for `addressed` the commit that did it when the changes show it. "
            "Judge only from what is given here; do not run anything.",
            f"Write the summaries and reasons in {profile.languages.internal}.",
            f"Head: {pr.head_sha}.",
            *blocks,
            schema_hint(CommentCheckResult.model_json_schema()),
        ]
    )


class CommentScan(Playbook):
    """The periodic comment check: open PRs with unresolved comments that were not
    checked at their head, highest priority first, at most
    ``review.comments.max_prs`` at a time."""

    name = "comment_scan"
    triggers = ()
    tick_names = ("comment_check",)

    def key(self, event: Event) -> str:
        return f"comment_scan:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        setting = profile.review.comments
        if setting.check != "scan":
            return PlaybookResult(result={"skipped": "checks run only when asked"})
        state_key = SCAN_STATE.format(repo=profile.repo)
        last_raw = await ctx.store.get_state(state_key)
        last = datetime.fromisoformat(last_raw) if last_raw else None
        now = ctx.clock.now()
        if not scan_due(setting.schedule, last, now):
            return PlaybookResult(result={"skipped": "not due"})
        rubric = rubric_of(profile)
        due = []
        for pr in await ctx.store.list_snapshots(profile.repo, open_only=True):
            if not in_scope(pr, profile):
                continue
            known = load_checks(
                await ctx.store.get_state(checks_key(pr.repo, pr.number))
            )
            if not any(
                t.id not in known or known[t.id].head_sha != pr.head_sha
                for t in open_threads(pr)
            ):
                continue
            evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
            due.append((priority_key(evaluation, rubric), pr))
        due.sort(key=lambda d: (d[0], -d[1].number))
        picked = [pr for _, pr in due[: setting.max_prs]]
        await ctx.store.set_state(state_key, now.isoformat())
        return PlaybookResult(
            events=[
                Event(
                    kind=EventKind.COMMENT_CHECK,
                    repo=pr.repo,
                    number=pr.number,
                    sha=pr.head_sha,
                    payload={"source": "scan"},
                    source="scan",
                )
                for pr in picked
            ],
            result={"checked": [pr.number for pr in picked], "due": len(due)},
        )
