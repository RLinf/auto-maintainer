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

"""Helpers for reading, deduplicating, and consolidating review lessons per repository
(design 5.11)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..domain.memory import MemoryChangeState, repo_dir
from ..profiles.knowledge import GLOBAL_LESSONS, LESSONS
from .base import PlaybookContext

LESSONS_HEADER = "# Review lessons\n"


def review_lessons_path(repo: str) -> str:
    """Memory path of ``repo``'s review lessons file."""
    return f"{repo_dir(repo)}/{LESSONS}"


def clean_lesson(text: str) -> str:
    """Trim surrounding whitespace and any leading markdown bullet prefix."""
    text = text.strip()
    if text.startswith(("- ", "* ")):
        text = text[2:].strip()
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def normalize_lesson(text: str) -> str:
    """Canonical form of a lesson for duplicate checks (case, whitespace, trailing
    sentence punctuation)."""
    return re.sub(r"\s+", " ", clean_lesson(text)).strip().rstrip(".。!！;；").lower()


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Split markdown into ``('other', raw_line)`` and ``('bullet', bullet_text)``
    entries, keeping indented continuation lines with their bullet."""
    blocks: list[tuple[str, str]] = []
    bullet: list[str] | None = None

    def flush() -> None:
        nonlocal bullet
        if bullet is not None:
            blocks.append(("bullet", "\n".join(bullet).strip()))
            bullet = None

    for raw in text.splitlines():
        if (raw.startswith("- ") or raw.startswith("* ")) and not raw.startswith("  "):
            flush()
            bullet = [raw[2:].strip()]
        elif bullet is not None and (raw.startswith("  ") or raw.startswith("\t")):
            bullet.append(raw.strip())
        else:
            flush()
            blocks.append(("other", raw))
    flush()
    return blocks


def _format_bullet(text: str) -> str:
    item = "\n  ".join(line.rstrip() for line in text.splitlines())
    return f"- {item}"


def parse_lessons(text: str | None) -> list[str]:
    """The bullet items in a lessons markdown file, in order."""
    if not text:
        return []
    return [
        body for kind, body in _split_blocks(text) if kind == "bullet" and body.strip()
    ]


async def pending_lesson_items(
    ctx: PlaybookContext, repo: str, existing: str
) -> list[str]:
    """Lessons proposed in open memory changes for ``repo``'s review lessons that are
    not already present in ``existing``."""
    path = review_lessons_path(repo)
    seen = {k for item in parse_lessons(existing) if (k := normalize_lesson(item))}
    pending: list[str] = []
    for change in await ctx.store.list_memory_changes(MemoryChangeState.OPEN):
        text = change.files.get(path)
        if not text:
            continue
        for item in parse_lessons(text):
            key = normalize_lesson(item)
            if key and key not in seen:
                seen.add(key)
                pending.append(item)
    return pending


async def global_lesson_items(
    ctx: PlaybookContext, use_global: bool
) -> tuple[str, list[str]]:
    """The shared global review lessons file and its bullets when ``use_global`` is
    enabled, else empty."""
    if not use_global:
        return "", []
    text = await ctx.memory.read_text(GLOBAL_LESSONS) or ""
    return text, parse_lessons(text)


def apply_lessons(
    current: str,
    updates: Sequence[tuple[str, Sequence[str]]],
    *,
    pending: Sequence[str] = (),
    global_lessons: Sequence[str] = (),
    header: str = LESSONS_HEADER,
) -> tuple[str | None, list[str]]:
    """Apply lesson ``updates`` (each ``(lesson, replaces)``) to ``current``.

    Deduplicates existing bullets in ``current``, skips any lesson already covered by
    ``current``, ``pending`` open changes, or ``global_lessons`` (by
    ``normalize_lesson``) unless ``replaces`` consolidates other bullets in ``current``,
    and removes every existing bullet named in ``replaces``. Returns ``(None, [])``
    when the file would not change.
    """
    blocks = (
        _split_blocks(current) if current.strip() else [("other", header.rstrip("\n"))]
    )
    current_keys = {
        k for kind, text in blocks if kind == "bullet" and (k := normalize_lesson(text))
    }
    pending_keys = {k for x in pending if (k := normalize_lesson(x))}
    global_keys = {k for x in global_lessons if (k := normalize_lesson(x))}

    remove_keys: set[str] = set()
    # Place each replacement at the first bullet it replaces so diffs stay local.
    replace_at: dict[str, list[str]] = {}
    appended: list[str] = []
    accepted: list[str] = []
    accepted_keys: set[str] = set()

    for raw_lesson, raw_replaces in updates:
        lesson = clean_lesson(raw_lesson)
        key = normalize_lesson(lesson)
        if not key or key in accepted_keys:
            continue
        matched = [
            k for r in raw_replaces if (k := normalize_lesson(r)) and k in current_keys
        ]
        replaces_other = any(k != key for k in matched)
        if not replaces_other and (
            key in current_keys or key in pending_keys or key in global_keys
        ):
            continue
        accepted.append(lesson)
        accepted_keys.add(key)
        if matched:
            remove_keys.update(matched)
            replace_at.setdefault(matched[0], []).append(lesson)
        else:
            appended.append(lesson)

    if not accepted:
        return None, []

    lines: list[str] = []
    emitted: set[str] = set()

    def emit_bullet(text: str) -> None:
        key = normalize_lesson(text)
        if not key or key in emitted:
            return
        emitted.add(key)
        lines.append(_format_bullet(text))

    for kind, text in blocks:
        if kind != "bullet":
            lines.append(text)
            continue
        key = normalize_lesson(text)
        if key in remove_keys:
            for replacement in replace_at.pop(key, ()):
                emit_bullet(replacement)
            continue
        emit_bullet(text)

    while lines and lines[-1] == "":
        lines.pop()

    for remaining in replace_at.values():
        for replacement in remaining:
            emit_bullet(replacement)
    for lesson in appended:
        emit_bullet(lesson)

    rendered = "\n".join(lines).rstrip("\n") + "\n"
    if current.strip() and rendered == current.rstrip("\n") + "\n":
        return None, []
    return rendered, accepted
