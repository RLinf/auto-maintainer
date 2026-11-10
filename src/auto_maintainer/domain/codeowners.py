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

"""CODEOWNERS parsing with gitignore-style patterns; the last matching rule wins."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from . import forges
from .ids import same_commit
from .snapshot import PrSnapshot, ReviewEventState

CODEOWNERS_LOCATIONS = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    ".gitlab/CODEOWNERS",
)
"""Every place a forge looks for CODEOWNERS, for reading all candidates; one
repository's own order is ``codeowners_locations``."""


def codeowners_locations(repo: str) -> tuple[str, ...]:
    """Where the forge ``repo`` is on looks for CODEOWNERS, first to last."""
    return forges.info_of(repo).codeowners


@dataclass(frozen=True)
class OwnerRule:
    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    directory = p.endswith("/")
    p = p.rstrip("/")
    out = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += r".*"
            i += 2
        elif p[i] == "*":
            out += r"[^/]*"
            i += 1
        elif p[i] == "?":
            out += r"[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    prefix = "^" if anchored else r"^(?:.*/)?"
    suffix = r"/.*$" if directory else r"(?:/.*)?$"
    return re.compile(prefix + out + suffix)


def parse_codeowners(text: str | None) -> list[OwnerRule]:
    rules = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rules.append(
            OwnerRule(
                parts[0], tuple(o.lstrip("@") for o in parts[1:]), _compile(parts[0])
            )
        )
    return rules


def owners_for(rules: list[OwnerRule], path: str) -> tuple[str, ...]:
    owners: tuple[str, ...] = ()
    for rule in rules:
        if rule.regex.match(path):
            owners = rule.owners
    return owners


def owners_of_paths(rules: list[OwnerRule], paths: list[str]) -> list[str]:
    """Distinct owners of any of ``paths``, in first-seen order, without the leading ``@``."""
    seen: list[str] = []
    for path in paths:
        for owner in owners_for(rules, path):
            if owner not in seen:
                seen.append(owner)
    return seen


class OwnerStanding(BaseModel):
    """One code owner of a PR's files and where they stand on it."""

    login: str
    """A user's login, an ``org/team``, or an email address, as CODEOWNERS names it."""
    team: bool = False
    author: bool = False
    """The PR's author, whose own approval GitHub does not count."""
    verdict: str | None = None
    """Their latest review that is not a comment: ``APPROVED``,
    ``CHANGES_REQUESTED``, or ``DISMISSED``; ``None`` before one."""
    at_head: bool = False
    """Whether that review was given at the PR's current head."""
    requested: bool = False
    """Whether GitHub asks them (or the team) for a review now."""
    files: int = 0


class OwnedPart(BaseModel):
    """Changed files that CODEOWNERS gives the same owners; GitHub wants an approval
    from one of them (other than the author) for the part to count as reviewed."""

    owners: list[str]
    patterns: list[str]
    files: list[str]
    approved_by: list[str] = Field(default_factory=list)
    """User owners whose latest verdict is an approval (at any head)."""


class CodeOwnerReport(BaseModel):
    parts: list[OwnedPart] = Field(default_factory=list)
    people: list[OwnerStanding] = Field(default_factory=list)
    unowned: list[str] = Field(default_factory=list)
    """Changed files no rule owns (or whose last matching rule names nobody)."""


def code_owner_report(rules: list[OwnerRule], pr: PrSnapshot) -> CodeOwnerReport:
    """Who owns the files ``pr`` changes, part by part (the last matching rule wins,
    as on GitHub), and where each owner stands: their latest verdict, whether GitHub
    asks them for a review, and how many of the files are theirs. A team's
    standing is only whether it is asked, since its members are not read."""
    verdicts: dict[str, tuple[str, bool]] = {}
    for review in pr.reviews:
        if review.state in (ReviewEventState.COMMENTED, ReviewEventState.PENDING):
            continue
        verdicts[review.author.lower()] = (
            review.state.value,
            same_commit(review.commit_sha, pr.head_sha),
        )
    asked_users = {r.lower() for r in pr.requested_reviewers}
    asked_teams = {t.lower().rsplit("/", 1)[-1] for t in pr.requested_teams}
    author = pr.author.lower()
    parts: dict[tuple[str, ...], OwnedPart] = {}
    unowned: list[str] = []
    for f in pr.files:
        rule = next((r for r in reversed(rules) if r.regex.match(f.path)), None)
        if rule is None or not rule.owners:
            unowned.append(f.path)
            continue
        part = parts.setdefault(
            rule.owners, OwnedPart(owners=list(rule.owners), patterns=[], files=[])
        )
        part.files.append(f.path)
        if rule.pattern not in part.patterns:
            part.patterns.append(rule.pattern)
    people: dict[str, OwnerStanding] = {}
    for part in parts.values():
        for owner in part.owners:
            key = owner.lower()
            team = "/" in owner
            verdict, at_head = verdicts.get(key, (None, False))
            standing = people.setdefault(
                key,
                OwnerStanding(
                    login=owner,
                    team=team,
                    author=key == author,
                    verdict=None if team else verdict,
                    at_head=at_head and not team,
                    requested=(key.rsplit("/", 1)[-1] in asked_teams)
                    if team
                    else key in asked_users,
                ),
            )
            standing.files += len(part.files)
            if (
                not team
                and key != author
                and verdict == ReviewEventState.APPROVED.value
                and owner not in part.approved_by
            ):
                part.approved_by.append(owner)
    return CodeOwnerReport(
        parts=list(parts.values()), people=list(people.values()), unowned=unowned
    )
