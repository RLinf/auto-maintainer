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

"""The priority model: rubrics, stage goals, evaluations, and tiers (design 5.6).

Everything here is internal. Scores, tiers, and rubric content never appear in
public text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .clock import as_utc, utc_now


class Dimension(BaseModel):
    weight: float = Field(ge=0.0, le=1.0)
    rule: str = ""


class Rubric(BaseModel):
    """Named dimensions with weights that sum to 1, and tier thresholds."""

    version: int = 1
    dimensions: dict[str, Dimension]
    tiers: dict[str, float] = Field(
        default_factory=lambda: {"P0": 78.0, "P1": 65.0, "P2": 52.0}
    )
    """Tier name to the lowest total it needs. The lowest tier is ``fallback_tier``."""
    fallback_tier: str = "P3"
    boost_cap: float = 5.0

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Rubric:
        total = sum(d.weight for d in self.dimensions.values())
        if not self.dimensions or abs(total - 1.0) > 0.011:
            raise ValueError(f"rubric weights must sum to 1.0, found {total:.3f}")
        return self

    def tier_names(self) -> list[str]:
        ordered = sorted(self.tiers, key=lambda t: -self.tiers[t])
        return [*ordered, self.fallback_tier]

    def tier_for(self, total: float) -> str:
        for name in sorted(self.tiers, key=lambda t: -self.tiers[t]):
            if total >= self.tiers[name]:
                return name
        return self.fallback_tier


DEFAULT_RUBRIC = Rubric(
    dimensions={
        "impact": Dimension(
            weight=0.30,
            rule="Wanted by many users, or unblocks important work.",
        ),
        "urgency": Dimension(
            weight=0.25,
            rule="Fixes silent wrong results, crashes, or regressions on core paths.",
        ),
        "strategic": Dimension(
            weight=0.25,
            rule="Serves the project's current goals, key collaborators, or ecosystem commitments.",
        ),
        "readiness": Dimension(
            weight=0.20,
            rule="Small, clean, and tested, or the author already addressed review.",
        ),
    }
)


class StageGoal(BaseModel):
    """A time-boxed priority that boosts matching PRs."""

    id: str
    title: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    boost: float = 2.5
    active: bool = True
    expires_at: datetime | None = None
    added_by: str = "service"
    added_at: datetime = Field(default_factory=utc_now)

    def is_live(self, now: datetime) -> bool:
        return self.active and (
            self.expires_at is None or as_utc(self.expires_at) > now
        )

    def matches(self, haystack: str) -> bool:
        text = haystack.lower()
        return any(k.lower() in text for k in self.keywords if k)


class Evaluation(BaseModel):
    """A PR's internal priority under its repository's rubric."""

    repo: str
    number: int
    title: str = ""
    rubric_version: int = 1
    scores: dict[str, float] = Field(default_factory=dict)
    summary: str = ""
    rationale: str = ""
    manual_boost: float = 0.0
    tier_override: str | None = None
    override_reason: str | None = None
    override_by: str | None = None
    override_at: datetime | None = None
    base: float = 0.0
    boost: float = 0.0
    total: float = 0.0
    tier: str = "P3"
    matched_goals: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utc_now)
    evaluated_by: str = "service"
    scored_sha: str | None = None
    source: str | None = None
    """Where the scores came from when imported, such as ``import:evaluations.json``."""

    @field_validator("scores")
    @classmethod
    def _scores_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        for name, score in value.items():
            if not 0.0 <= float(score) <= 10.0:
                raise ValueError(
                    f"score for {name!r} must be in [0, 10], found {score}"
                )
        return {k: round(float(v), 1) for k, v in value.items()}


def recompute(
    evaluation: Evaluation,
    rubric: Rubric,
    goals: list[StageGoal],
    haystack: str,
    now: datetime | None = None,
) -> Evaluation:
    """Return ``evaluation`` with base, boost, total, and tier recomputed.

    ``haystack`` is the text stage goals match against: title, summary, labels, and
    review state. Missing dimension scores count as zero.
    """
    now = now or utc_now()
    base = round(
        10.0
        * sum(
            d.weight * evaluation.scores.get(name, 0.0)
            for name, d in rubric.dimensions.items()
        ),
        1,
    )
    matched = [g for g in goals if g.is_live(now) and g.matches(haystack)]
    boost = round(min(sum(g.boost for g in matched), rubric.boost_cap), 1)
    total = round(max(0.0, min(100.0, base + boost + evaluation.manual_boost)), 1)
    tier = (
        evaluation.tier_override
        if evaluation.tier_override in rubric.tier_names()
        else rubric.tier_for(total)
    )
    return evaluation.model_copy(
        update={
            "base": base,
            "boost": boost,
            "total": total,
            "tier": tier,
            "matched_goals": [g.id for g in matched],
            "rubric_version": rubric.version,
        }
    )


def missing_dimensions(evaluation: Evaluation, rubric: Rubric) -> list[str]:
    """Dimensions of ``rubric`` that ``evaluation`` has no score for."""
    return [d for d in rubric.dimensions if d not in evaluation.scores]


def rubric_from_data(data: dict[str, Any] | None) -> Rubric:
    """Build a rubric from YAML data, or return the default rubric for ``None``."""
    return DEFAULT_RUBRIC if not data else Rubric.model_validate(data)
