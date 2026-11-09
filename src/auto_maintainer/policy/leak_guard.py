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

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_CODE_RE = re.compile(r"`[^`\n]*`")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MENTION_RE = re.compile(
    r"(?<![\w@/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?)(?![\w-])"
)
_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_SCORE_RE = re.compile(r"\b(score|tier)\s*[:=]\s*\d", re.IGNORECASE)


TIER_BLIND = frozenset({ActionKind.REVIEW_POST, ActionKind.REVIEW_UPDATE})
"""Action kinds whose text comes from agents never shown a PR's priority tier or
score, so a tier name in it is the review's own severity label. Scores stay
refused there (``_SCORE_RE``)."""


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
    """Text outside code blocks, inline code, and HTML comments."""
    return _CODE_RE.sub(" ", _FENCE_RE.sub(" ", _COMMENT_RE.sub(" ", text)))


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
) -> GuardResult:
    """What is wrong with ``text`` as public text; ``tiers`` refuses tier names."""
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

    if profile.languages.public.lower().startswith("en"):
        letters = [c for c in prose if c.isalpha()]
        cjk = len(_CJK_RE.findall(prose))
        if letters and cjk / len(letters) > 0.02:
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
) -> tuple[PublicText, list[str]]:
    """Return ``PublicText`` and style warnings, or raise ``LeakError``."""
    result = check_text(text, profile, allowed_mentions, internal_entities, tiers=tiers)
    if not result.ok:
        raise LeakError(result.errors)
    return PublicText(text, profile.repo, _key=GUARD_KEY), result.warnings
