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

"""Tidying a repository's lessons with an agent (design 5.11): lessons are approved
one at a time, so a file collects repeats and lessons a later one replaced. Reviewers
talk about one file with an agent on the Lessons tab (``web/lessons_talk.py``): it
summarizes the file (repeats merged, related lessons under topics, each standing on
its own, nothing added, every dropped lesson listed with why) and revises it as they
ask. What it writes is a T2 memory change that never merges on its own: a maintainer
of the repository reads it on the tab and applies or discards it. A newer revision
replaces the draft that waits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.lessons import LessonsFile, Section, parse, render
from ..domain.memory import MemoryChange, MemoryChangeState, repo_dir
from ..domain.ports import Store
from ..profiles.knowledge import LESSON_FILES

SUMMARY_TITLE = "lessons: summarize"
"""How the agent's drafts are titled, so the tab shows them as the agent's."""

READERS = {
    "review": "every review, follow-up, triage, and quality score",
    "priority": "priority scoring",
    "agent": "the agents' work in it (reviews, reproductions, fixes, CI diagnoses)",
}

TALK = (
    "You keep the {heading} of {repo}: rules its maintainers approved one at a time, "
    "which {readers} there reads. Its reviewers talk with you here to tidy them: "
    "summarize, reword, merge, regroup, add, or drop lessons. To summarize, rewrite "
    "them as one tidy list: merge lessons that say the same thing, put related "
    "lessons under short topics, and word each so it stands on its own. Keep every "
    "rule that still holds, with its specifics (paths, labels, numbers), and add none "
    "of your own unless a reviewer states one; drop a lesson only when another says "
    "it, a later one replaces it, or a reviewer asks, and list each dropped lesson "
    "with why. When your answer changes the lessons, return the whole list in "
    "`groups`; when it does not, leave `groups` null. Nothing applies until a "
    "maintainer applies your draft. {no_code} Write in {language}."
)


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
    why: str = Field(description="Which lesson says it, or replaces it, or who asked.")


class LessonsTurn(BaseModel):
    """One answer in the conversation about a lessons file."""

    reply: str = Field(description="The answer to the newest message.")
    groups: list[LessonGroup] | None = Field(
        default=None,
        description="The whole revised list when this answer changes the lessons; "
        "null when they stay as they are.",
    )
    dropped: list[DroppedLesson] = Field(default_factory=list)


def lessons_path(repo: str, kind: str) -> str:
    return f"{repo_dir(repo)}/{LESSON_FILES[kind][0]}"


async def base_lessons(
    store: Store, memory_text: str, repo: str, kind: str
) -> tuple[LessonsFile, MemoryChange | None]:
    """What a revision builds on: the newest draft of the file that nobody approved
    yet, else the file as applied (``memory_text``); with that draft."""
    path, heading = lessons_path(repo, kind), LESSON_FILES[kind][1]
    for change in sorted(
        await store.list_memory_changes(MemoryChangeState.OPEN),
        key=lambda c: c.created_at,
        reverse=True,
    ):
        if change.files.get(path) is not None and not change.approvals:
            return parse(change.files[path] or "", heading), change
    return parse(memory_text, heading), None


def revised_lessons(current: LessonsFile, groups: list[LessonGroup]) -> LessonsFile:
    return LessonsFile(
        heading=current.heading,
        sections=[
            Section(title=g.title.strip(), items=[x for x in g.lessons if x.strip()])
            for g in groups
        ],
    )


def unchanged(current: LessonsFile, after: LessonsFile) -> bool:
    return render(current) == render(after)
