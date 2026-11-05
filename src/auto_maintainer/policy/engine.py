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

"""The policy engine (design 5.8): a pure function from an action and facts to a decision."""

from __future__ import annotations

import fnmatch

from pydantic import BaseModel, Field

from ..domain.actions import Action, ActionKind, Autonomy, Decision, DecisionKind
from ..domain.profile import Conditions, RepoProfile


class PolicyFacts(BaseModel):
    """What the policy engine needs to know beyond the action and the profile."""

    dev_mode: bool = False
    """Cap every repository action at shadow (a development instance)."""
    outside_sandbox: bool = False
    """A development instance with a sandbox repository acts on nothing else."""
    paused: bool = False
    """The service or this repository is paused."""
    approved_by: str | None = None
    """Set when deciding again for a proposal a maintainer approved."""
    author_is_team: bool | None = None
    recommendation: str | None = None
    """``ready``, ``changes``, or ``blocked``, when the action follows a review."""
    changed_paths: list[str] = Field(default_factory=list)
    executed_on_sha: int = 0
    """Executed actions of the same kind on the same PR and head SHA."""
    public_writes_last_hour: int = 0
    repo_public_writes_last_hour: int = 0
    max_public_writes_per_hour: int = 120
    max_repo_public_writes_per_hour: int = 40


ONE_PER_SHA = {ActionKind.REVIEW_POST}
"""Kinds limited to one execution per PR and head SHA (later changes edit in place)."""

WRITE_CAP_RULE = "limit.write_cap"


def _conditions_hold(conditions: Conditions, facts: PolicyFacts) -> tuple[bool, str]:
    if conditions.author == "team" and facts.author_is_team is not True:
        return False, "the author is not on the team"
    if conditions.author == "external" and facts.author_is_team is not False:
        return False, "the author is on the team"
    if conditions.recommendation and facts.recommendation != conditions.recommendation:
        return False, f"the review recommendation is not {conditions.recommendation}"
    if conditions.paths:
        outside = [
            p
            for p in facts.changed_paths
            if not any(fnmatch.fnmatch(p, g) for g in conditions.paths)
        ]
        if outside or not facts.changed_paths:
            return False, "changed files fall outside the allowed paths"
    return True, ""


def decide(action: Action, profile: RepoProfile, facts: PolicyFacts) -> Decision:
    kind = action.kind
    public = kind.touches_repository

    if facts.outside_sandbox:
        return Decision(
            kind=DecisionKind.DENY,
            rule="dev_mode.sandbox",
            reason="a development instance acts only on its sandbox repository",
        )
    if kind in ONE_PER_SHA and facts.executed_on_sha >= 1:
        return Decision(
            kind=DecisionKind.DENY,
            rule="limit.one_per_sha",
            reason=f"{kind} already ran for this head SHA",
        )
    if public and (
        facts.public_writes_last_hour >= facts.max_public_writes_per_hour
        or facts.repo_public_writes_last_hour >= facts.max_repo_public_writes_per_hour
    ):
        return Decision(
            kind=DecisionKind.DENY,
            rule=WRITE_CAP_RULE,
            reason="the hourly public write cap was reached",
        )

    rule = profile.rule(kind)
    holds, why_not = _conditions_hold(rule.conditions, facts)
    level = rule.level if holds else rule.otherwise
    rule_name = f"policy.{kind}={level}" + ("" if holds else f" ({why_not})")
    if facts.dev_mode and public:
        level = level.cap(Autonomy.SHADOW)
        rule_name = f"dev_mode caps {kind} at shadow"

    if level == Autonomy.OFF:
        return Decision(
            kind=DecisionKind.DENY,
            rule=rule_name,
            reason=f"{kind} is off for {profile.repo}",
        )
    if level == Autonomy.SHADOW:
        return Decision(kind=DecisionKind.SHADOW, rule=rule_name)
    if facts.paused and public:
        return Decision(
            kind=DecisionKind.PROPOSE,
            rule="paused",
            reason="the service is paused; held for a maintainer",
        )
    if level == Autonomy.PROPOSE and not facts.approved_by:
        return Decision(kind=DecisionKind.PROPOSE, rule=rule_name)
    reason = f"approved by {facts.approved_by}" if level == Autonomy.PROPOSE else ""
    return Decision(kind=DecisionKind.EXECUTE, rule=rule_name, reason=reason)
