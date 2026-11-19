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

"""Follow-up review (design 5.6): the review skill on the PR again, for the status of
every earlier finding and the new ones, in two turns like a first review
(``review_form``)."""

from __future__ import annotations

from pathlib import Path

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
from .base import (
    PR_EVENTS,
    Playbook,
    PlaybookContext,
    PlaybookResult,
    in_scope,
    keeps_session,
)
from .feedback import Rejection, declined_labels, rejections_block, rejections_on
from .person_notes import load_notes, notes_block
from .prompts import (
    FOLLOWUP_FORM,
    NOTHING_RUNS,
    accepted_labels,
    checkout_line,
    forge_note,
    form_prompt,
    label_block,
    label_choices,
    own_review_skill,
    quality_block,
    recommendations_line,
    review_call,
    review_instructions_block,
    review_rules,
    untrusted,
    work_lessons_block,
    writing_block,
)
from .requests import FOLLOWUP_STATES, review_key, review_request, starts_review
from .review import reproduction_record
from .review_form import fill_form, forget_report, keep_report, saved_report
from .review_session import (
    ReviewMachine,
    close,
    earlier_work,
    review_machine,
    session_task_fields,
    testing_block,
)


class Followup(Playbook):
    name = "followup"
    triggers = (*PR_EVENTS, EventKind.DASHBOARD_COMMAND, EventKind.PR_COMMENT)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return starts_review(event, profile)

    def key(self, event: Event) -> str:
        return review_key(self.name, event)

    async def run(self, ctx: PlaybookContext, wake: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        # The request that started the job; later events (its session opening) wake it.
        event = Event.model_validate(ctx.job.events[0]) if ctx.job.events else wake
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
        if request is not None and (request.own or request.full):
            return PlaybookResult(
                result={
                    "skipped": "a full review asked for"
                    if request.full
                    else "a review of the person's own",
                    "handed_to": "review",
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
            return PlaybookResult(
                result={"skipped": f"state is {cls.state}", "handed_to": "review"}
            )
        previous_sha = cls.reviewed_sha
        prior = await self._prior_findings(ctx, pr)

        info = await ctx.forge.get_repo(pr.repo)
        turned_down = await rejections_on(ctx.store, pr.repo, pr.number)
        declined = declined_labels(turned_down)
        choices = [
            c for c in label_choices(info.labels, profile, pr) if c not in declined
        ]
        limit = profile.review.max_suggested_labels
        # A report this job's agent wrote already needs only its form filled in.
        answer = await saved_report(ctx, pr.head_sha)
        machine = (
            ReviewMachine()
            if answer is not None
            else await review_machine(
                ctx,
                pr,
                profile,
                event.id,
                ctx.requested_by,
                timed_out=wake.kind == EventKind.JOB_TIMEOUT,
            )
        )
        if machine.wait is not None:
            return machine.wait
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        try:
            if answer is None:
                answer = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.FOLLOWUP,
                        prompt=await self._prompt(
                            ctx,
                            pr,
                            profile,
                            info.default_branch,
                            workdir,
                            machine,
                            previous_sha,
                            prior,
                            spot=cls.state == ReviewState.APPROVED_NEW_COMMITS,
                            turned_down=turned_down,
                        ),
                        workdir=workdir,
                        skill_dirs=ctx.skill_dirs(),
                        repo=pr.repo,
                        messages=True,
                        **session_task_fields(ctx, machine),
                    )
                )
                if not answer.ok:
                    raise RuntimeError(f"follow-up agent failed: {answer.error}")
                await close(ctx, machine)
                await keep_report(ctx, pr.head_sha, answer)
            await ctx.keep_work_session(answer, workdir)
            result = await fill_form(
                ctx,
                answer,
                FollowupResult,
                form_prompt(
                    FOLLOWUP_FORM.format(recommendations=recommendations_line(profile)),
                    answer.text,
                    [
                        label_block(choices, limit),
                        quality_block(profile, followup=True),
                    ],
                ),
                workdir,
            )
        finally:
            await ctx.workspace.release(workdir)
            # A run the job's next try (or the next start) picks up keeps its session.
            if not keeps_session(ctx, answer):
                await close(ctx, machine)
        await forget_report(ctx)

        result.labels = accepted_labels(result.labels, choices, limit)
        meaning = profile.review.meaning_of(result.recommendation)
        rendered = render_followup(result, pr.head_sha, previous_sha, profile)
        team = ctx.trusted_input
        hints = ActionHints(
            author_is_team=team, recommendation=meaning, changed_paths=pr.paths
        )
        # A follow-up someone asked for drafts for them under a key of its own, as a
        # requested review does: under the commit's key alone it was taken for the
        # follow-up a scan recorded at that commit, and dropped with its draft.
        asked = f"req:{event.id}" if request is not None and request.manual else ""
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
                discriminator=asked,
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
                    discriminator=f"{asked}:followup" if asked else "followup",
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
                    discriminator=f"{asked}:followup:labels"
                    if asked
                    else "followup:labels",
                    rationale=f"labels the follow-up suggests: {', '.join(result.labels)}",
                    hints=hints,
                    draft_for=request.by if request else None,
                )
            )
        return PlaybookResult(
            actions=actions,
            result={
                "statuses": len(result.statuses),
                "new": len(result.new_findings),
                "labels": result.labels,
                **machine.summary(pr.head_sha),
            },
        )

    async def _prompt(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        profile: RepoProfile,
        base_ref: str,
        workdir: Path,
        machine: ReviewMachine,
        previous_sha: str | None,
        prior: str,
        *,
        spot: bool,
        turned_down: list[Rejection],
    ) -> str:
        """What the follow-up agent reads: the skill to use on the PR again, the
        commit it was last reviewed at with the findings then, and only what the
        skill cannot know."""
        since = f"`{short_sha(previous_sha)}`" if previous_sha else "an earlier commit"
        what = (
            f"It was approved at {since}, and new commits came since: check only "
            "whether they bring a regression or a new problem."
            if spot
            else f"It was reviewed at {since}, and new commits came since: say for "
            "each earlier finding below whether they resolved it, partly addressed "
            "it, or left it open, then review what changed for new problems."
        )
        if previous_sha:
            what += f" `git diff {previous_sha}..HEAD` shows what changed."
        if machine.token:
            testing = machine.testing(ctx, reproduce=False)
            earlier = await earlier_work(ctx, pr)
            testing = f"{testing}\n\n{earlier}" if earlier else testing
        else:
            testing = NOTHING_RUNS.format(
                note=machine.note or "No test machine is attached to this review."
            )
        lessons = await ctx.knowledge.review_lessons(pr.repo, profile)
        return "\n\n".join(
            part
            for part in [
                review_call(
                    profile, ctx.service.skills_dir, workdir, pr, "review again"
                ),
                what,
                checkout_line(pr.head_sha, base_ref),
                review_rules(profile, ctx.requested_by),
                testing,
                testing_block(await reproduction_record(ctx.store, pr.repo, pr.number)),
                "The earlier findings:\n"
                + untrusted(
                    "earlier.findings",
                    prior
                    or "(no structured findings; read the earlier review comments)",
                    30_000,
                ),
                review_instructions_block(profile),
                writing_block(profile, own_review_skill(profile, workdir) is not None),
                f"The maintainers' lessons for reviews of this repository:\n{lessons}"
                if lessons
                else "",
                notes_block(await load_notes(ctx.store, ctx.requested_by), "reviews")
                if ctx.requested_by
                else "",
                work_lessons_block(
                    await ctx.knowledge.agent_lessons(
                        pr.repo, [machine.lab] if machine.lab else []
                    )
                ),
                rejections_block(turned_down),
                forge_note(pr),
            ]
            if part
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
