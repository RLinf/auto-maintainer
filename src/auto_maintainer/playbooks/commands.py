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

"""Slash commands in PR comments (design 5.3). ``review`` and ``rerun`` go to their playbooks."""

from __future__ import annotations

from ..domain.actions import (
    ActionKind,
    ActionStatus,
    CommentUpsertPayload,
    LabelPayload,
    make_action,
)
from ..domain.events import Event, EventKind
from ..domain.merge_gate import MergeArm
from ..domain.profile import RepoProfile
from ..domain.records import AuditEntry
from .base import Playbook, PlaybookContext, PlaybookResult
from .merge import arm_allowed

HELP = """Commands, one per line:
- `{p} review`: ask for a fresh review of the current head.
- `{p} rerun`: rerun failed CI jobs (the PR author may do this once per commit).
- `{p} merge`: merge when every check passes and the PR is approved. `{p} merge cancel` stops it.
- `{p} label <name> ...`: add labels.
- `{p} why`: explain the service's last action on this PR.
- `{p} pause`: pause the service on this repository."""

_PUBLIC_KINDS = [
    ActionKind.LABEL_APPLY,
    ActionKind.LABEL_CI_GATE,
    ActionKind.REVIEW_POST,
    ActionKind.REVIEW_UPDATE,
    ActionKind.COMMENT_UPSERT,
    ActionKind.CI_RERUN_FAILED,
    ActionKind.PR_REQUEST_REVIEWERS,
    ActionKind.PR_UPDATE_BRANCH,
    ActionKind.PR_MERGE,
    ActionKind.PR_NUDGE,
]


class Commands(Playbook):
    name = "commands"
    triggers = (EventKind.PR_COMMENT,)

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        names = {c.get("name") for c in event.payload.get("commands", [])}
        return bool(names - {"review", "rerun"})

    def key(self, event: Event) -> str:
        return f"commands:{event.repo}#{event.number}:{event.payload.get('comment_id')}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        author = event.payload.get("author", "")
        prefix = profile.comments.command_prefix
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None:
            return PlaybookResult(result={"skipped": "PR not found"})
        replies: list[str] = []
        actions = []
        events: list[Event] = []
        for command in event.payload.get("commands", []):
            name, args = command.get("name"), command.get("args", [])
            if name in ("review", "rerun"):
                continue
            if name == "help":
                replies.append(HELP.format(p=prefix))
                continue
            if not await ctx.may_command(author):
                replies.append(
                    f"`{prefix} {name}` needs write access to this repository."
                )
                continue
            if name == "merge":
                if not profile.merge.enabled:
                    replies.append("Merge scheduling is off for this repository.")
                    continue
                allowed, permission, owner = await arm_allowed(ctx, author, pr.paths)
                if not allowed:
                    replies.append(
                        "Scheduling a merge here needs a maintainer of this repository."
                    )
                    continue
                now = ctx.clock.now()
                await ctx.store.save_arm(
                    MergeArm(
                        repo=pr.repo,
                        number=pr.number,
                        sha=pr.head_sha,
                        armed_by=author,
                        armed_at=now,
                        armer_permission=permission,
                        armer_is_codeowner=owner,
                    )
                )
                events.append(
                    Event(
                        kind=EventKind.MERGE_ARMED,
                        repo=pr.repo,
                        number=pr.number,
                        sha=pr.head_sha,
                        payload={"by": author, "armed_at": now.isoformat()},
                    )
                )
                replies.append(
                    f"Merge scheduled at `{pr.head_sha[:7]}` by {author}. It merges on its turn once the checklist holds."
                )
            elif name == "merge_cancel":
                await ctx.store.delete_arm(pr.repo, pr.number)
                events.append(
                    Event(
                        kind=EventKind.MERGE_DISARMED,
                        repo=pr.repo,
                        number=pr.number,
                        payload={"by": author},
                    )
                )
                replies.append(f"Merge cancelled by {author}.")
            elif name == "label" and args:
                actions.append(
                    make_action(
                        ActionKind.LABEL_APPLY,
                        pr.repo,
                        LabelPayload(labels=args),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=f"cmd:{event.payload.get('comment_id')}",
                    )
                )
            elif name == "why":
                replies.append(await self._why(ctx, pr.repo, pr.number))
            elif name == "pause":
                record = await ctx.store.get_repo(pr.repo)
                if record is not None:
                    record.paused = True
                    await ctx.store.save_repo(record)
                    await ctx.store.add_audit(
                        AuditEntry(
                            kind="pause",
                            repo=pr.repo,
                            actor=author,
                            summary=f"paused by {prefix} pause on #{pr.number}",
                        )
                    )
                replies.append(
                    f"Paused on this repository by {author}. Maintainers can resume it from the dashboard."
                )
        for action in actions:
            action.provenance.requested_by = author
        if replies:
            actions.append(
                make_action(
                    ActionKind.COMMENT_UPSERT,
                    pr.repo,
                    CommentUpsertPayload(
                        purpose=f"command-{event.payload.get('comment_id')}",
                        body="\n\n".join(replies),
                    ),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator=str(event.payload.get("comment_id")),
                )
            )
        return PlaybookResult(
            actions=actions, events=events, result={"replies": len(replies)}
        )

    async def _why(self, ctx: PlaybookContext, repo: str, number: int) -> str:
        records = await ctx.store.list_actions(
            repo=repo,
            number=number,
            kinds=_PUBLIC_KINDS,
            statuses=[ActionStatus.EXECUTED],
            limit=1,
        )
        if not records:
            return "The service has not acted on this PR yet."
        r = records[0]
        who = f", approved by {r.approver}" if r.approver else ""
        return f"Last action: `{r.action.kind}` at `{(r.action.head_sha or '')[:7]}`{who}, because {r.action.rationale or 'its playbook asked for it'}."
