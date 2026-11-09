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

"""Learning from a rejection (design 5.11): a reviewer's reason for turning a proposal
down becomes a review lesson when it holds for the whole repository.

The PR itself already remembers every rejection (``playbooks.feedback``); this
playbook asks an agent whether the reason says more than that. A lesson is T1
knowledge in ``knowledge/review_lessons.md``, which every review, follow-up, triage,
and quality agent of the repository reads. It carries nobody's approval: the agent
wrote its words, so the memory gate merges it under the repository's T1 rules, after
the notice window where the repository lets knowledge merge on its own, else once a
maintainer approves it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, ProposalState, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, Tier, repo_dir
from ..domain.profile import RepoProfile
from ..domain.records import ChatMessage, ChatThread
from .base import Playbook, PlaybookContext, PlaybookResult
from .feedback import EXCERPT_CHARS
from .prompts import schema_hint, untrusted

LESSONS_HEADER = "# Review lessons\n"


class LearnResult(BaseModel):
    disposition: Literal["PR_SPECIFIC", "LESSON", "ALREADY_KNOWN"] = Field(
        description="PR_SPECIFIC: the reason is about this PR only. LESSON: it states "
        "a rule for this repository's future PRs. ALREADY_KNOWN: a current lesson says it."
    )
    lesson: str = Field(
        "",
        description="For LESSON: the rule in one or two sentences that stand on their "
        "own, naming the labels or kinds of change it is about; no PR number.",
    )
    why: str = Field("", description="One sentence on the choice.")
    message: str = Field(
        "",
        description="For LESSON: a short note to the reviewer, in the internal "
        "language, quoting the lesson and saying it applies to later reviews once "
        "the repository's knowledge rules let it in.",
    )


class LearnFromRejection(Playbook):
    """Turn a rejection's reason into a review lesson when it holds beyond the PR."""

    name = "learn"
    triggers = (EventKind.PROPOSAL_DECIDED,)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.payload.get("state") == "rejected" and bool(event.repo)

    def key(self, event: Event) -> str:
        return f"learn:{event.repo}:{event.payload.get('proposal')}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo
        proposal = await ctx.store.get_proposal(str(event.payload.get("proposal")))
        if proposal is None or proposal.state != ProposalState.REJECTED:
            return PlaybookResult(result={"skipped": "not a rejected proposal"})
        record = await ctx.store.get_action(proposal.action_id)
        reason = (proposal.reason or "").strip()
        if record is None or not reason:
            return PlaybookResult(result={"skipped": "no reason to learn from"})
        action = record.action
        payload = action.payload
        if action.kind == ActionKind.LABEL_APPLY:
            proposed = "the labels " + ", ".join(payload.get("labels") or [])
        elif isinstance(payload.get("body"), str):
            proposed = f"a {action.kind} beginning:\n{payload['body'][:EXCERPT_CHARS]}"
        else:
            proposed = f"a {action.kind}"
        pr = (
            await ctx.store.get_snapshot(event.repo, action.number)
            if action.number
            else None
        )
        path = f"{repo_dir(event.repo)}/knowledge/review_lessons.md"
        current = await ctx.memory.read_text(path) or LESSONS_HEADER
        prompt = "\n\n".join(
            [
                f"A reviewer of {event.repo} turned down something the service "
                f"proposed on PR #{action.number} and said why. The PR remembers "
                "this already. Decide whether the reason also teaches a rule for this "
                "repository's future PRs, such as what a label means here.",
                "Never turn a reason about this PR alone into a rule. Write the lesson "
                f"in {profile.languages.internal}.",
                untrusted("pr.title", pr.title if pr else "", 300),
                f"What was proposed: {untrusted('proposal', proposed, 2000)}",
                f"The reviewer's reason: {untrusted('reviewer.reason', reason, 2000)}",
                f"The repository's current review lessons:\n{untrusted('lessons', current, 8000)}",
                schema_hint(LearnResult.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.LEARN,
            prompt=prompt,
            workdir=ctx.scratch(),
            output_schema=LearnResult.model_json_schema(),
            repo=event.repo,
        )
        answer = await ctx.ask(task, LearnResult)
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"learning agent failed: {answer.error}")
        result = LearnResult.model_validate(answer.structured)
        done = {"disposition": result.disposition, "why": result.why}
        if result.disposition != "LESSON" or not result.lesson.strip():
            return PlaybookResult(result=done)

        lesson = result.lesson.strip()
        item = "\n  ".join(line.rstrip() for line in lesson.splitlines())
        by = proposal.decided_by or "a reviewer"
        change = MemoryChange(
            tier=Tier.T1,
            title=f"lesson from {by}'s rejection on #{action.number}",
            description=f"{lesson}\n\nFrom the reason: {reason}",
            files={path: current.rstrip() + f"\n- {item}\n"},
            repos=[event.repo],
            requested_by=proposal.decided_by,
            created_at=ctx.clock.now(),
        )
        await ctx.store.save_memory_change(change)
        memory = make_action(
            ActionKind.MEMORY_CHANGE,
            event.repo,
            MemoryChangePayload(change_id=change.id),
            discriminator=change.id,
        )
        if proposal.decided_by:
            thread = ChatThread(
                owner=proposal.decided_by,
                title=f"Lesson: {event.repo}#{action.number}",
                repo=event.repo,
            )
            await ctx.store.save_thread(thread)
            await ctx.store.add_message(
                ChatMessage(
                    thread_id=thread.id,
                    role="assistant",
                    text=result.message.strip() or lesson,
                    proposals=[memory.id],
                )
            )
        return PlaybookResult(actions=[memory], result={**done, "change": change.id})
