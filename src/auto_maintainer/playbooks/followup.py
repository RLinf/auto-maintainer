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

"""Follow-up review (design 5.6): the status of every earlier finding, plus new ones."""

from __future__ import annotations

from ..domain.actions import (
    ActionHints,
    ActionKind,
    LabelPayload,
    ReviewPostPayload,
    ThreadReplyPayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
from ..domain.markers import parse_markers
from ..domain.profile import RepoProfile
from ..domain.review import (
    FollowupResult,
    ReviewState,
    classify_review_state,
    render_followup,
)
from ..domain.snapshot import PrSnapshot, PrState
from .base import PR_EVENTS, Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import (
    accepted_labels,
    headless,
    interactive,
    label_block,
    label_choices,
    pr_block,
    reproduction_block,
    review_skill_block,
    schema_hint,
    untrusted,
)
from .reproduce import auto_reproduction
from .requests import FOLLOWUP_STATES, review_key, review_request, starts_review
from .review import reproduce_after, reproduction_record


class Followup(Playbook):
    name = "followup"
    triggers = (*PR_EVENTS, EventKind.DASHBOARD_COMMAND, EventKind.PR_COMMENT)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return starts_review(event, profile)

    def key(self, event: Event) -> str:
        return review_key(self.name, event)

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        request = review_request(event)
        if request is not None and request.manual:
            ctx.requested_by = request.by
            ctx.agent = request.agent
        manual = request is not None and request.manual
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if not manual and not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        if (
            request is not None
            and request.source == "comment"
            and not await ctx.may_command(request.by or "")
        ):
            return PlaybookResult(
                result={"skipped": "the commenter may not request reviews"}
            )
        if (
            request is None or request.source == "scan"
        ) and not profile.review.scan.followups:
            return PlaybookResult(
                result={
                    "skipped": "this repository does not re-review updated PRs on its own"
                }
            )
        ctx.trusted_input = await ctx.author_is_team(pr)
        cls = classify_review_state(
            pr,
            ctx.identities(),
            profile.comments.legacy_markers,
            profile.scope.include_drafts or manual,
            own_actions=ctx.own_reviews,
        )
        if cls.state not in FOLLOWUP_STATES:
            return PlaybookResult(result={"skipped": f"state is {cls.state}"})
        previous_sha = cls.reviewed_sha
        prior = await self._prior_findings(ctx, pr)

        info = await ctx.forge.get_repo(pr.repo)
        choices = label_choices(info.labels, profile, pr)
        limit = profile.review.max_suggested_labels
        repro = await auto_reproduction(ctx, pr, profile, ctx.trusted_input)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            incremental = (
                await ctx.workspace.diff(workdir, previous_sha, pr.head_sha)
                if previous_sha
                else ""
            )
            full = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            spot = cls.state == ReviewState.APPROVED_NEW_COMMITS
            prompt = "\n\n".join(
                [
                    interactive(profile, ctx.requested_by)
                    if ctx.requested_by
                    else headless(profile),
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    (
                        "This PR was approved earlier; new commits arrived since. Check only whether they introduce "
                        "a regression or a new problem. Report earlier findings only if they apply."
                        if spot
                        else "This is a follow-up review. For every earlier finding, decide whether the new commits "
                        "resolved it (`resolved`, with the fixing commit), partly addressed it (`partial`), or left it "
                        "open (`open`). Then report new findings introduced since the last reviewed commit."
                    ),
                    untrusted(
                        "earlier.findings",
                        prior
                        or "(no structured findings; read the earlier review comments)",
                        30_000,
                    ),
                    f"Last reviewed commit: {previous_sha or 'unknown'}. Head: {pr.head_sha}.",
                    reproduction_block(
                        await reproduction_record(ctx.store, pr.repo, pr.number),
                        upcoming=repro.upcoming(),
                        no_machine=repro.no_machine,
                    ),
                    untrusted(
                        "diff.since_review", incremental or "(not available)", 80_000
                    ),
                    pr_block(pr, full, 60_000),
                    "`recommendation` must be one of: "
                    + ", ".join(
                        repr(v) for v in profile.review.recommendations.values()
                    ),
                    label_block(choices, limit),
                    schema_hint(FollowupResult.model_json_schema()),
                ]
            )
            task = AgentTask(
                kind=TaskKind.FOLLOWUP,
                prompt=prompt,
                workdir=workdir,
                output_schema=FollowupResult.model_json_schema(),
                skill_dirs=ctx.skill_dirs(),
                repo=pr.repo,
            )
            answer = await ctx.ask(task, FollowupResult)
            if not answer.ok or answer.structured is None:
                raise RuntimeError(f"follow-up agent failed: {answer.error}")
            result = FollowupResult.model_validate(answer.structured)
        finally:
            await ctx.workspace.release(workdir)

        result.labels = accepted_labels(result.labels, choices, limit)
        meaning = profile.review.meaning_of(result.recommendation)
        rendered = render_followup(result, pr.head_sha, previous_sha, profile)
        team = ctx.trusted_input
        hints = ActionHints(
            author_is_team=team, recommendation=meaning, changed_paths=pr.paths
        )
        actions = [
            make_action(
                ActionKind.REVIEW_POST,
                pr.repo,
                ReviewPostPayload(
                    body=rendered.body,
                    comments=rendered.comments,
                    recommendation=result.recommendation,
                    runtime=answer.runtime,
                    kind="followup",
                    # The last reviewed commit, so a revised follow-up renders the same.
                    result={
                        **result.model_dump(mode="json"),
                        "previous_sha": previous_sha,
                    },
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                rationale=f"follow-up since {short_sha(previous_sha)}",
                hints=hints,
                draft_for=request.by if request else None,
            )
        ]
        for status in result.statuses:
            if status.status != "resolved":
                continue
            comment_id = self._thread_for(ctx, pr, status.id)
            if comment_id:
                actions.append(
                    make_action(
                        ActionKind.THREAD_REPLY,
                        pr.repo,
                        ThreadReplyPayload(
                            comment_id=comment_id,
                            body=f"Resolved in `{short_sha(status.fixed_in or pr.head_sha)}`.",
                        ),
                        number=pr.number,
                        head_sha=pr.head_sha,
                        discriminator=status.id,
                        hints=hints,
                        draft_for=request.by if request else None,
                    )
                )
        if meaning == "ready":
            actions.append(
                make_action(
                    ActionKind.LABEL_APPLY,
                    pr.repo,
                    LabelPayload(remove_roles=["changes_requested"]),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator="followup",
                    hints=hints,
                    draft_for=request.by if request else None,
                )
            )
        if result.labels:
            actions.append(
                make_action(
                    ActionKind.LABEL_APPLY,
                    pr.repo,
                    LabelPayload(labels=result.labels),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator="followup:labels",
                    rationale=f"labels the follow-up suggests: {', '.join(result.labels)}",
                    hints=hints,
                    draft_for=request.by if request else None,
                )
            )
        return PlaybookResult(
            actions=actions,
            events=reproduce_after(repro, pr, actions[0].id, request),
            result={
                "statuses": len(result.statuses),
                "new": len(result.new_findings),
                "labels": result.labels,
                **({"reproduce_on": repro.lab} if repro.lab else {}),
            },
        )

    async def _prior_findings(self, ctx: PlaybookContext, pr: PrSnapshot) -> str:
        records = await ctx.store.list_review_records(pr.repo, pr.number)
        lines: list[str] = []
        for record in records:
            for f in record.result.get("findings", []) or []:
                lines.append(
                    f"{f.get('id')} [{f.get('severity')}] {f.get('title')} ({f.get('path')}:{f.get('line')}): {f.get('body', '')[:400]}"
                )
            for f in record.result.get("new_findings", []) or []:
                lines.append(
                    f"{f.get('id')} [{f.get('severity')}] {f.get('title')}: {f.get('body', '')[:400]}"
                )
        if lines:
            return "\n".join(dict.fromkeys(lines))
        ids = {i.lower() for i in ctx.identities()}
        legacy = ctx.require_profile().comments.legacy_markers
        texts = [
            c.body
            for c in pr.comments
            if c.author.lower() in ids
            and any(
                m.kind in ("review", "followup") for m in parse_markers(c.body, legacy)
            )
        ]
        texts += [r.body for r in pr.reviews if r.author.lower() in ids and r.body]
        return "\n\n---\n\n".join(texts)

    def _thread_for(
        self, ctx: PlaybookContext, pr: PrSnapshot, finding_id: str
    ) -> int | None:
        ids = {i.lower() for i in ctx.identities()}
        for thread in pr.threads:
            if thread.is_resolved or not thread.comments:
                continue
            first = thread.comments[0]
            if first.author.lower() in ids and first.body.startswith(
                f"**{finding_id} ·"
            ):
                already = any(
                    c.author.lower() in ids and "Resolved in" in c.body
                    for c in thread.comments[1:]
                )
                if not already and not same_commit(first.commit_sha, pr.head_sha):
                    return first.id
        return None
