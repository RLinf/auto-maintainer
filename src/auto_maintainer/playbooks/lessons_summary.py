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

"""Summarizing a repository's lessons (design 5.11): lessons are approved one at a
time, so a file collects repeats and lessons a later one replaced. On a person's
request (the Lessons tab, ``dashboard.command`` with ``command: lessons``), an agent
rewrites one file as a tidy list: repeats merged, related lessons under topics, each
standing on its own, nothing added. What it drops it lists with why.

The result is a T2 memory change that never merges on its own: a maintainer of the
repository reads it on the Lessons tab and applies or discards it. A newer summary of
the same file replaces one that still waits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.lessons import LessonsFile, Section, parse, render
from ..domain.memory import MemoryChange, MemoryChangeState, Tier, repo_dir
from ..profiles.knowledge import LESSON_FILES
from .base import Playbook, PlaybookContext, PlaybookResult
from .prompts import schema_hint, untrusted

COMMAND = "lessons"
SUMMARY_TITLE = "lessons: summarize"
"""How a summary's memory change is titled, so a newer one finds it to replace."""

READERS = {
    "review": "every review, follow-up, triage, and quality score",
    "priority": "priority scoring",
    "agent": "the agents' work in it (reviews, reproductions, fixes, CI diagnoses)",
}


class LessonGroup(BaseModel):
    title: str = Field(
        "",
        description="A short topic for the lessons under it; empty for lessons that "
        "fit no topic.",
    )
    lessons: list[str] = Field(
        description="Each lesson as one or a few sentences that stand on their own; "
        "a lesson may keep a short numbered list."
    )


class DroppedLesson(BaseModel):
    lesson: str = Field(description="The lesson as the file has it.")
    why: str = Field(description="Which lesson says it, or replaces it.")


class SummarizedLessons(BaseModel):
    groups: list[LessonGroup]
    dropped: list[DroppedLesson] = Field(default_factory=list)
    summary: str = Field(
        description="For the maintainers, in a few sentences: what was merged, "
        "grouped, reworded, or dropped."
    )


def lessons_path(repo: str, kind: str) -> str:
    return f"{repo_dir(repo)}/{LESSON_FILES[kind][0]}"


class SummarizeLessons(Playbook):
    """Have an agent tidy one lessons file into a draft a maintainer applies."""

    name = "lessons"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: object) -> bool:
        return (
            event.payload.get("command") == COMMAND
            and event.payload.get("kind") in LESSON_FILES
            and bool(event.repo and event.payload.get("by"))
        )

    def key(self, event: Event) -> str:
        return f"lessons:{event.repo}:{event.payload.get('kind')}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo
        repo, kind, by = (
            event.repo,
            str(event.payload["kind"]),
            str(event.payload["by"]),
        )
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        path = lessons_path(repo, kind)
        heading = LESSON_FILES[kind][1]
        text = await ctx.memory.read_text(path) or ""
        current = parse(text, heading)
        if len(current.items()) < 2:
            return PlaybookResult(result={"skipped": "fewer than two lessons"})
        prompt = "\n\n".join(
            [
                f"These are the {heading.lower()} of {repo}: rules its maintainers "
                f"approved one at a time, which {READERS[kind]} there reads. Rewrite "
                "them as one tidy list: merge lessons that say the same thing, put "
                "related lessons under short topics, and word each so it stands on "
                "its own. Keep every rule that still holds, with its specifics "
                "(paths, labels, numbers); add none of your own. Drop a lesson only "
                "when another says it or a later one replaces it, and list each "
                f"dropped lesson with why. Write in {profile.languages.internal}.",
                untrusted("lessons", text, 40_000),
                schema_hint(SummarizedLessons.model_json_schema()),
            ]
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.LEARN,
                prompt=prompt,
                workdir=ctx.scratch(),
                output_schema=SummarizedLessons.model_json_schema(),
                repo=repo,
                requested_by=by,
                activity="lessons",
            ),
            SummarizedLessons,
        )
        if not answer.ok or answer.structured is None:
            return PlaybookResult(failed=f"the agent did not summarize: {answer.error}")
        result = SummarizedLessons.model_validate(answer.structured)
        after = LessonsFile(
            heading=current.heading,
            sections=[
                Section(
                    title=g.title.strip(), items=[x for x in g.lessons if x.strip()]
                )
                for g in result.groups
            ],
        )
        if not after.items():
            return PlaybookResult(failed="the agent's summary kept no lesson")
        rendered = render(after)
        if rendered == render(current):
            return PlaybookResult(result={"unchanged": True})
        dropped = "\n".join(
            f"- {d.lesson.strip()} ({d.why.strip()})" for d in result.dropped
        )
        waiting = [
            c
            for c in await ctx.store.list_memory_changes(MemoryChangeState.OPEN)
            if c.title.startswith(SUMMARY_TITLE) and path in c.files and not c.approvals
        ]
        change = MemoryChange(
            tier=Tier.T2,
            title=f"{SUMMARY_TITLE} {heading.lower()} of {repo}",
            description="\n\n".join(
                part
                for part in [
                    result.summary.strip(),
                    f"Dropped:\n{dropped}" if dropped else "",
                    f"Asked for by {by} on the Lessons tab.",
                ]
                if part
            ),
            files={path: rendered},
            repos=[repo],
            requested_by=by,
            from_own_signals=False,
            created_at=ctx.clock.now(),
        )
        await ctx.store.save_memory_change(change)
        return PlaybookResult(
            actions=[
                make_action(
                    ActionKind.MEMORY_CHANGE,
                    repo,
                    MemoryChangePayload(
                        change_id=change.id,
                        replaces=waiting[-1].id if waiting else None,
                    ),
                    discriminator=change.id,
                )
            ],
            result={
                "change": change.id,
                "before": len(current.items()),
                "after": len(after.items()),
                "dropped": len(result.dropped),
            },
        )
