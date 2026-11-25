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

"""The leak guard (design 5.8): the only way to build ``PublicText``.

It rejects text that names internal fields or entities, mentions people outside
the allowed set, or is not in the repository's public language. Style phrases
produce warnings, which the proposal shows to maintainers.

Priority tier names (``P0``, ``P1``, ...) count as a leak only in text that an
agent who knew the PR's tier could have written. Reviews are not: the agents that
write and revise them are never shown a PR's priority, and review guides often
rank findings on the same ``P0``-``P3`` scale (``TIER_BLIND``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..domain.actions import ActionKind
from ..domain.profile import RepoProfile
from ..domain.scoring import Rubric
from ..domain.secrets import find_secrets
from ..domain.visibility import GUARD_KEY, PublicText

_FENCE_OPEN_RE = re.compile(r"[ \t]*((?:>[ \t]*)*)(`{3,}|~{3,})([^\n]*)")
_PARA_BREAK_RE = re.compile(r"\r?\n(?:[ \t]*>)*[ \t]*\r?\n")
_BLOCKQUOTE_RE = re.compile(r"(?m)^[ \t]*>.*$")
_MD_LINK_RE = re.compile(r"\[[^\]\n]+\]\([^)\n]+\)")
_URL_RE = re.compile(r"https?://\S+")
_SUB_RE = re.compile(r"<sub>.*?</sub>", re.DOTALL | re.IGNORECASE)
_QUOTED_RE = re.compile(
    r'"[^"\n]*"'
    r"|“[^”\n]*”"
    r"|(?<!\w)'[^'\n]*'(?!\w)"
    r"|‘[^’\n]*’"
    r"|「[^」\n]*」"
    r"|『[^』\n]*』"
    r"|《[^》\n]*》"
    r"|〈[^〉\n]*〉"
    r"|【[^】\n]*】"
)
_MENTION_RE = re.compile(
    r"(?<![\w@/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?)(?![\w-])"
)
_CJK_CLASS = r"㐀-䶿一-鿿぀-ヿ가-힯"
_CJK_RE = re.compile(rf"[{_CJK_CLASS}]")
_CJK_RUN_RE = re.compile(rf"[{_CJK_CLASS}]{{12,}}")
_SCORE_RE = re.compile(r"\b(score|tier)\s*[:=]\s*\d", re.IGNORECASE)


def mentioned(text: str) -> set[str]:
    """The logins ``text`` mentions, lowercased, outside code and comments."""
    return {login.lower() for login in _MENTION_RE.findall(_prose(text))}


TIER_BLIND = frozenset(
    {ActionKind.REVIEW_POST, ActionKind.REVIEW_UPDATE, ActionKind.PR_MERGE}
)
"""Action kinds whose text never comes from an agent shown a PR's priority tier or
score: reviews (whose guides rank findings on ``P0``-``P3``) and merge commit
messages (built from the PR's own title and commits). Scores stay refused there
(``_SCORE_RE``)."""


class LeakError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class GuardResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _prose(text: str) -> str:
    """Text outside fenced code blocks, inline code spans, and HTML comments."""
    out: list[str] = []
    n = len(text)
    i = 0
    at_line_start = True

    while i < n:
        if at_line_start:
            fence = _FENCE_OPEN_RE.match(text, i)
            if fence is not None:
                prefix, marker, info = fence.groups()
                ch = marker[0]
                if ch == "~" or "`" not in info:
                    line_end = text.find("\n", fence.end())
                    if line_end != -1:
                        depth = prefix.count(">")
                        close_re = re.compile(
                            rf"^[ \t]*(?:>[ \t]*){{{depth}}}{re.escape(ch)}{{{len(marker)},}}[ \t\r]*$",
                            re.MULTILINE,
                        )
                        closed = close_re.search(text, line_end + 1)
                        if closed is not None:
                            end = closed.end()
                            if end < n and text[end] == "\n":
                                end += 1
                            out.append("\n")
                            i = end
                            at_line_start = True
                            continue

        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end != -1:
                comment_end = end + 3
                out.append("\n" if "\n" in text[i:comment_end] else " ")
                i = comment_end
                at_line_start = False
                continue

        if text[i] == "\\" and i + 1 < n and text[i + 1] == "`":
            out.append(text[i : i + 2])
            i += 2
            at_line_start = False
            continue

        if text[i] == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            ticks = j - i
            para_end = _PARA_BREAK_RE.search(text, j)
            limit = para_end.start() if para_end is not None else n
            k = j
            matched_end = -1
            while k < limit:
                k = text.find("`", k, limit)
                if k == -1:
                    break
                k_end = k
                while k_end < limit and text[k_end] == "`":
                    k_end += 1
                if k_end - k == ticks:
                    matched_end = k_end
                    break
                k = k_end
            if matched_end != -1:
                out.append(" ")
                i = matched_end
            else:
                out.append(text[i:j])
                i = j
            at_line_start = False
            continue

        ch = text[i]
        out.append(ch)
        i += 1
        at_line_start = ch == "\n"

    return "".join(out)


def _strip_quoted(prose: str) -> str:
    """Prose without blockquotes, Markdown links, URLs, or single-line quotes."""
    text = _BLOCKQUOTE_RE.sub(" ", prose)
    text = _MD_LINK_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    return _QUOTED_RE.sub(" ", text)


def _has_cjk_prose(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cjk = len(_CJK_RE.findall(text))
    if not cjk:
        return False
    if cjk / len(letters) > 0.15:
        return True
    if _CJK_RUN_RE.search(text):
        return True
    for line in text.splitlines():
        line_cjk = len(_CJK_RE.findall(line))
        if not line_cjk:
            continue
        line_letters = sum(1 for c in line if c.isalpha())
        if not line_letters:
            continue
        ratio = line_cjk / line_letters
        if (line_cjk >= 4 and ratio > 0.5) or (line_cjk >= 8 and ratio > 0.25):
            return True
    return False


def _not_english(prose: str) -> bool:
    """Whether ``prose`` is written in a CJK language rather than English.

    Quotes, blockquotes, and Markdown links are discounted when the text also
    has unquoted prose, so English reviews on bilingual repositories can cite
    short non-English titles or snippets without being blocked.
    """
    body = _SUB_RE.sub(" ", prose)
    own = _strip_quoted(body)
    target = own if any(c.isalpha() for c in own) else _URL_RE.sub(" ", body)
    return _has_cjk_prose(target)


def _tier_names(profile: RepoProfile) -> list[str]:
    rubric = profile.priority.rubric
    if isinstance(rubric, Rubric):
        return rubric.tier_names()
    return ["P0", "P1", "P2", "P3"]


def check_text(
    text: str,
    profile: RepoProfile,
    allowed_mentions: set[str],
    internal_entities: list[str],
    *,
    tiers: bool = True,
    language: bool = True,
) -> GuardResult:
    """What is wrong with ``text`` as public text; ``tiers`` refuses tier names,
    ``language`` enforces ``profile.languages.public``."""
    result = GuardResult()
    for kind in find_secrets(text):
        result.errors.append(f"contains what looks like a {kind}")
    prose = _prose(text)
    lowered = prose.lower()

    for term in profile.confidentiality.internal_terms:
        if term and re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", lowered):
            result.errors.append(f"mentions the internal term {term!r}")
    for name in internal_entities:
        if (
            name
            and len(name) >= 3
            and re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", lowered)
        ):
            result.errors.append(f"names the internal entity {name!r}")
    for tier in _tier_names(profile) if tiers else []:
        if re.search(rf"(?<![\w-]){re.escape(tier)}(?![\w-])", prose):
            result.errors.append(f"mentions the priority tier {tier!r}")
    if _SCORE_RE.search(prose):
        result.errors.append("contains what looks like a score or tier value")

    allowed = {m.lower() for m in allowed_mentions}
    for login in _MENTION_RE.findall(prose):
        if login.lower() not in allowed:
            result.errors.append(
                f"mentions @{login}, who is not the PR author or a code owner of the touched paths"
            )

    if (
        language
        and profile.languages.public.lower().startswith("en")
        and _not_english(prose)
    ):
        result.errors.append(
            f"is not in the public language ({profile.languages.public})"
        )

    for phrase in profile.languages.style_phrases:
        if phrase and phrase.lower() in lowered:
            result.warnings.append(f"style: avoid {phrase!r}")
    return result


def make_public(
    text: str,
    profile: RepoProfile,
    allowed_mentions: set[str],
    internal_entities: list[str],
    *,
    tiers: bool = True,
    language: bool = True,
) -> tuple[PublicText, list[str]]:
    """Return ``PublicText`` and style warnings, or raise ``LeakError``."""
    result = check_text(
        text,
        profile,
        allowed_mentions,
        internal_entities,
        tiers=tiers,
        language=language,
    )
    if not result.ok:
        raise LeakError(result.errors)
    return PublicText(text, profile.repo, _key=GUARD_KEY), result.warnings
