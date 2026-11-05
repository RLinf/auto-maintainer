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

"""Merge (design 5.5): wait for the gate on an armed PR, then merge it."""

from __future__ import annotations

from ..domain.actions import (
    ActionHints,
    ActionKind,
    CommentUpsertPayload,
    LabelPayload,
    MergePayload,
    UpdateBranchPayload,
    make_action,
)
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
from ..domain.merge_gate import GateResult, evaluate_merge_gate, squash_message
from ..domain.permissions import Permission
from ..domain.profile import RepoProfile
from ..domain.review import ReviewState
from ..domain.snapshot import PrState
from .base import Playbook, PlaybookContext, PlaybookResult, Wait
from .hygiene import hygiene_key

WAIT_KINDS = [
    EventKind.CHECKS_COMPLETED,
    EventKind.PR_REVIEW_SUBMITTED,
    EventKind.PR_SYNCHRONIZED,
    EventKind.PR_LABELED,
    EventKind.PR_UPDATED,
    EventKind.PR_CLOSED,
    EventKind.MERGE_DISARMED,
    EventKind.ACTION_EXECUTED,
    EventKind.ACTION_FAILED,
    EventKind.PR_RECONCILED,
]
WAIT_SECONDS = 6 * 3600


def render_gate(gate: GateResult) -> str:
    lines = ["### Merge checklist", ""]
    for item in gate.items:
        mark = "✅" if item.ok else ("⏳" if item.pending else "❌")
        lines.append(
            f"- {mark} {item.name.replace('_', ' ')}"
            + (f": {item.detail}" if item.detail else "")
        )
    return "\n".join(lines)


class MergePlaybook(Playbook):
    name = "merge"
    triggers = (EventKind.MERGE_ARMED,)

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return bool(profile and profile.merge.enabled and event.number)

    def key(self, event: Event) -> str:
        return f"merge:{event.repo}#{event.number}:{event.payload.get('armed_at', event.id)}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        repo, number = event.repo, event.number
        arm = await ctx.store.get_arm(repo, number)
        if arm is None or event.kind == EventKind.MERGE_DISARMED:
            return PlaybookResult(result={"done": "disarmed"})
        pr = await ctx.forge.get_pr(repo, number)
        if pr is None or pr.state != PrState.OPEN:
            await ctx.store.delete_arm(repo, number)
            return PlaybookResult(
                result={"done": f"PR is {pr.state if pr else 'gone'}"}
            )
        if (
            event.kind == EventKind.ACTION_EXECUTED
            and event.payload.get("kind") == ActionKind.PR_MERGE
        ):
            return PlaybookResult(result={"done": "merged"})
        wait = Wait(kinds=WAIT_KINDS, seconds=WAIT_SECONDS, reason="merge gate")

        if not same_commit(arm.sha, pr.head_sha):
            cls = ctx.classify(pr)
            records = await ctx.store.list_review_records(repo, number)
            spot = next(
                (r for r in reversed(records) if same_commit(r.sha, pr.head_sha)), None
            )
            approved = cls.state != ReviewState.FOLLOWUP_REVIEW and any(
                r.state.value == "APPROVED" for r in pr.reviews
            )
            clean = spot is not None and not any(
                f.get("severity") in ("critical", "major")
                for f in spot.result.get("new_findings", []) or []
            )
            if approved and clean:
                arm = arm.model_copy(update={"sha": pr.head_sha})
                await ctx.store.save_arm(arm)
            elif spot is None and event.kind != EventKind.JOB_TIMEOUT:
                return PlaybookResult(
                    wait=wait.model_copy(
                        update={
                            "seconds": 2 * 3600,
                            "reason": "waiting for the follow-up spot-check",
                        }
                    )
                )
            else:
                await ctx.store.delete_arm(repo, number)
                body = (
                    f"New commits arrived after {arm.armed_by} armed this merge at `{short_sha(arm.sha)}`, "
                    f"so the merge is cancelled. Arm it again at `{short_sha(pr.head_sha)}` when it is ready."
                )
                return PlaybookResult(
                    actions=[
                        make_action(
                            ActionKind.COMMENT_UPSERT,
                            repo,
                            CommentUpsertPayload(purpose="merge", body=body),
                            number=number,
                            head_sha=pr.head_sha,
                            discriminator="disarmed",
                        )
                    ],
                    result={"done": "disarmed after push"},
                )

        info = await ctx.forge.get_repo(repo)
        reviewers = {r.author for r in pr.reviews}
        perms = {login: await ctx.permission(login) for login in reviewers}
        hygiene = await ctx.store.get_state(hygiene_key(repo, number)) or {}
        blocking = {
            r["id"]: r["passed"]
            for r in hygiene.get("results", [])
            if r.get("blocking")
            and not r.get("skipped")
            and same_commit(hygiene.get("sha"), pr.head_sha)
        }
        missing_hygiene = [
            s.id
            for s in profile.checks
            if s.blocking and s.enabled and s.id not in blocking
        ]
        for check_id in missing_hygiene:
            blocking[check_id] = False
        paused = await self._paused(ctx, repo)
        gate = evaluate_merge_gate(
            pr,
            profile,
            info,
            arm,
            perms,
            ctx.identities(),
            blocking,
            paused,
            ctx.clock.now(),
        )
        hints = ActionHints(
            author_is_team=await ctx.author_is_team(pr), changed_paths=pr.paths
        )

        if gate.outcome == "merge":
            subject, body = squash_message(pr, profile)
            action = make_action(
                ActionKind.PR_MERGE,
                repo,
                MergePayload(
                    method=profile.merge.method,
                    subject=subject,
                    body=body,
                    armed_by=arm.armed_by,
                ),
                number=number,
                head_sha=pr.head_sha,
                rationale=f"merge gate holds; armed by {arm.armed_by}",
                hints=hints,
            )
            action.provenance.requested_by = arm.armed_by
            return PlaybookResult(
                actions=[action],
                wait=wait.model_copy(
                    update={"action_ids": [action.id], "kinds": WAIT_KINDS}
                ),
            )
        if gate.outcome == "update_branch":
            action = make_action(
                ActionKind.PR_UPDATE_BRANCH,
                repo,
                UpdateBranchPayload(),
                number=number,
                head_sha=pr.head_sha,
                hints=hints,
            )
            return PlaybookResult(actions=[action], wait=wait)
        actions = []
        if gate.needs_ci_gate:
            actions.append(
                make_action(
                    ActionKind.LABEL_CI_GATE,
                    repo,
                    LabelPayload(roles=["ci_gate"]),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator="merge",
                    hints=hints,
                )
            )
        if gate.outcome == "blocked":
            actions.append(
                make_action(
                    ActionKind.COMMENT_UPSERT,
                    repo,
                    CommentUpsertPayload(purpose="merge", body=render_gate(gate)),
                    number=number,
                    head_sha=pr.head_sha,
                    discriminator=",".join(i.name for i in gate.failing()),
                    hints=hints,
                )
            )
        return PlaybookResult(
            actions=actions,
            wait=wait,
            result={
                "outcome": gate.outcome,
                "failing": [i.name for i in gate.failing()],
            },
        )

    async def _paused(self, ctx: PlaybookContext, repo: str) -> bool:
        if await ctx.store.get_state("paused"):
            return True
        record = await ctx.store.get_repo(repo)
        return bool(record and record.paused)


async def arm_allowed(
    ctx: PlaybookContext, login: str, paths: list[str]
) -> tuple[bool, Permission, bool]:
    """Whether ``login`` may arm a merge under the profile's ``merge.arming.who``."""
    profile = ctx.require_profile()
    permission = await ctx.permission(login)
    owner = login in await ctx.codeowners_for(paths)
    who = profile.merge.arming.who
    allowed = (
        owner and permission.at_least(Permission.WRITE)
        if who == "codeowners"
        else permission.at_least(Permission(who))
    )
    return allowed, permission, owner
