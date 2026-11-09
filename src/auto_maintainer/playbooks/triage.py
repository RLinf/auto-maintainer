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

"""Triage: review state, a one-sentence summary, and labels from the profile's rules."""

from __future__ import annotations

import fnmatch
import re

from pydantic import BaseModel, Field

from ..domain.actions import ActionHints, ActionKind, LabelPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import LabelRule
from ..domain.review import ReviewState
from ..domain.scoring import Evaluation
from ..domain.snapshot import PrSnapshot
from .base import PR_EVENTS, Playbook, PlaybookContext, PlaybookResult, in_scope
from .feedback import declined_labels, rejections_block, rejections_on
from .prompts import pr_block, schema_hint


class TriageAnswer(BaseModel):
    summary: str = Field(
        description="One or two sentences on what the PR does, in the internal language."
    )
    labels: list[str] = Field(
        default_factory=list, description="Labels from the allowed list only."
    )


def rule_labels(pr: PrSnapshot, rules: list[LabelRule]) -> list[str]:
    title = pr.title.lower()
    found: list[str] = []
    for rule in rules:
        hit = any(title.startswith(p.lower()) for p in rule.title_prefixes)
        hit = hit or any(
            re.search(rf"(?<![\w-])(?:{k})(?![\w-])", title) for k in rule.keywords
        )
        hit = hit or any(
            fnmatch.fnmatch(path, g) for g in rule.paths for path in pr.paths
        )
        if hit and rule.label not in found:
            found.append(rule.label)
    return found


class Triage(Playbook):
    name = "triage"
    triggers = PR_EVENTS
    pool = "agent"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        state = ctx.classify(pr).state
        rules = await ctx.knowledge.label_rules(pr.repo, profile)
        turned_down = await rejections_on(ctx.store, pr.repo, pr.number)
        declined = declined_labels(turned_down)
        labels = [lbl for lbl in rule_labels(pr, rules) if lbl not in declined]
        roles: list[str] = []
        remove: list[str] = []
        if state in (ReviewState.NEW_REVIEW, ReviewState.FOLLOWUP_REVIEW):
            roles.append("needs_review")
        team = await ctx.author_is_team(pr)
        ctx.trusted_input = team
        if not team:
            roles.append("community")

        evaluation = await ctx.store.get_evaluation(pr.repo, pr.number)
        summary = evaluation.summary if evaluation else ""
        if not summary or event.kind == EventKind.PR_OPENED:
            info = await ctx.forge.get_repo(pr.repo)
            taxonomy = sorted(({r.label for r in rules} or set(info.labels)) - declined)
            notes = [
                rejections_block(turned_down),
                await ctx.knowledge.review_lessons(pr.repo, profile),
            ]
            answer = await self._ask(
                ctx, pr, taxonomy, profile.languages.internal, notes
            )
            if answer is not None:
                summary = answer.summary
                labels += [
                    lbl
                    for lbl in answer.labels
                    if lbl in taxonomy and lbl not in labels
                ]
                evaluation = (
                    evaluation or Evaluation(repo=pr.repo, number=pr.number)
                ).model_copy(update={"summary": summary, "title": pr.title})
                await ctx.store.save_evaluation(evaluation)

        actions = []
        if labels or roles:
            actions.append(
                make_action(
                    ActionKind.LABEL_APPLY,
                    pr.repo,
                    LabelPayload(roles=roles, labels=labels, remove_roles=remove),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator="triage",
                    rationale=f"review state {state}; label rules matched {labels}",
                    hints=ActionHints(author_is_team=team, changed_paths=pr.paths),
                )
            )
        return PlaybookResult(
            actions=actions,
            result={
                "state": state,
                "labels": labels,
                "roles": roles,
                "summary": summary,
            },
        )

    async def _ask(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        taxonomy: list[str],
        language: str,
        notes: list[str],
    ) -> TriageAnswer | None:
        """``notes``: what reviewers turned down on the PR, and the repository's
        review lessons."""
        rejected, lessons = notes
        workdir = ctx.scratch()
        prompt = "\n\n".join(
            [
                "Summarize this pull request for the maintainers and pick labels.",
                f"Write the summary in {language}. Pick labels only from this list: {taxonomy}.",
                f"Review lessons from this repository's maintainers:\n{lessons}"
                if lessons
                else "",
                rejected,
                pr_block(pr),
                schema_hint(TriageAnswer.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.TRIAGE,
            prompt=prompt,
            workdir=workdir,
            output_schema=TriageAnswer.model_json_schema(),
            repo=pr.repo,
        )
        result = await ctx.ask(task, TriageAnswer)
        if not result.ok or result.structured is None:
            return None
        return TriageAnswer.model_validate(result.structured)
