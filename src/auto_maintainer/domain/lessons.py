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

"""A lessons file of the memory repo (``knowledge/*_lessons.md``) as lessons:
``# heading``, optional ``## topic`` sections, and one ``- `` item per lesson,
whose further lines are indented two spaces. Agents read the file whole; the
dashboard lists, edits, and summarizes its lessons (design 5.11)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class Section(BaseModel):
    """Lessons under one topic; the first section of a file may have no title."""

    title: str = ""
    items: list[str] = Field(default_factory=list)


@dataclass
class LessonsFile:
    heading: str
    sections: list[Section] = field(default_factory=list)

    def items(self) -> list[str]:
        return [item for s in self.sections for item in s.items]


def parse(text: str, heading: str) -> LessonsFile:
    """The lessons of ``text``; ``heading`` when it has none of its own. A line
    before any item (prose under a heading) counts as an item of its own."""
    found = LessonsFile(heading=heading, sections=[Section()])
    current: list[str] | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            found.sections[-1].items.append("\n".join(current).strip())
        current = None

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("# "):
            close()
            found.heading = line[2:].strip() or heading
        elif line.startswith("## "):
            close()
            found.sections.append(Section(title=line[3:].strip()))
        elif line.startswith("- ") or line.strip() == "-":
            close()
            current = [line[2:].rstrip()]
        elif current is not None:
            current.append(
                line[2:].rstrip() if line.startswith("  ") else line.rstrip()
            )
        else:
            current = [line.strip()]
    close()
    found.sections = [s for i, s in enumerate(found.sections) if s.items or i > 0]
    return found


def render(lessons: LessonsFile) -> str:
    """The file's text: each item's further lines indented, so a line of a lesson
    never starts an item or a heading of its own."""
    out = [f"# {lessons.heading}"]
    for section in lessons.sections:
        items = [item.strip() for item in section.items if item.strip()]
        if section.title.strip():
            out += ["", f"## {section.title.strip()}"]
        elif not items:
            continue
        for item in items:
            first, *rest = item.splitlines()
            out.append(f"- {first.strip()}")
            out += [f"  {line.rstrip()}" for line in rest if line.strip()]
    return "\n".join(out).rstrip() + "\n"


def key(lesson: str) -> str:
    """A lesson's words, for matching it across versions of its file."""
    return " ".join(lesson.split())
