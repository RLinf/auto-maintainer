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

"""The repository profile schema (design 2 and 8.2).

A ``RepoProfile`` is the merged, validated settings of one repository. Defaults
here are safe for any repository: every action that touches a repository is in
``shadow`` unless a layer says otherwise.
"""

from __future__ import annotations

import fnmatch
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .actions import ActionKind, Autonomy
from .ids import check_repo
from .permissions import Permission
from .scoring import Rubric

PROFILE_SCHEMA = 1

ROLES = (
    "needs_review",
    "changes_requested",
    "ci_gate",
    "reproduced",
    "needs_docs",
    "community",
    "automerge",
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(_Section):
    min_pr: int = 0
    base_branches: list[str] = Field(default_factory=list)
    """Glob patterns; empty means every base branch."""
    include_drafts: bool = False
    backfill_reviews: bool = False
    """Review PRs that were open before the repository was added; off by default to bound cost."""
    ignore_authors: list[str] = Field(
        default_factory=lambda: ["dependabot[bot]", "renovate[bot]"]
    )
    issues: bool = False

    def base_in_scope(self, base: str) -> bool:
        return not self.base_branches or any(
            fnmatch.fnmatch(base, p) for p in self.base_branches
        )


class People(_Section):
    codeowners: str | None = None
    """Path of the CODEOWNERS file; ``None`` searches the usual locations."""
    team: list[str] = Field(default_factory=list)
    team_min_permission: Permission = Permission.WRITE
    """Authors with at least this permission count as the team."""
    review_identities: list[str] = Field(default_factory=list)
    """Logins whose reviews count as the service's own, besides the service's own login."""


class Access(_Section):
    configure: Permission = Permission.ADMIN
    """Who configures a repository whose People record names no maintainers."""

    @field_validator("configure")
    @classmethod
    def _not_below_write(cls, value: Permission) -> Permission:
        if not Permission(value).at_least(Permission.WRITE):
            raise ValueError(f"access.configure must be write or higher, found {value}")
        return value


class TitleRule(_Section):
    pattern: str | None = None
    max_description: int | None = None
    lowercase_start: bool = False
    ascii_only: bool = False
    no_trailing_period: bool = False


class LicenseHeader(_Section):
    pattern: str
    paths: list[str] = Field(default_factory=lambda: ["**/*.py"])


class Contribution(_Section):
    commit_convention: Literal["none", "conventional"] = "none"
    conventional_types: list[str] = Field(
        default_factory=lambda: [
            "feat",
            "fix",
            "docs",
            "style",
            "refactor",
            "perf",
            "test",
            "build",
            "ci",
            "chore",
            "revert",
        ]
    )
    title_rule: TitleRule | None = None
    signoff: bool = False
    template_sections: list[str] = Field(default_factory=list)
    license_header: LicenseHeader | None = None


class CheckSpec(_Section):
    id: str
    type: Literal["builtin", "command", "agent"] = "builtin"
    name: str | None = None
    """For ``builtin``: the check type; defaults to ``id``."""
    run: str | None = None
    """For ``command``: a command line with ``{title}``, ``{body_file}``, ... placeholders."""
    prompt: str | None = None
    """For ``agent``: what the agent judges."""
    blocking: bool = False
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


DEFAULT_SECTIONS = ["Summary", "Findings", "Tests and docs", "Recommendation"]


class ReviewScan(_Section):
    schedule: str = "0 */6 * * *"
    """Cron (UTC) for the periodic scan when ``review.trigger`` is ``scan``."""
    max_prs: int = 5
    """At most this many PRs per scan, highest priority first."""
    followups: bool = True
    """Re-review reviewed PRs that received new commits, in scans and, with
    ``review.trigger: events``, as the commits arrive."""

    @field_validator("schedule")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        from croniter import croniter

        if not croniter.is_valid(value):
            raise ValueError(
                f"review.scan.schedule is not a valid cron expression: {value!r}"
            )
        return value


class CommentChecks(_Section):
    check: Literal["scan", "manual"] = "scan"
    """``scan``: check open PRs' unresolved review comments against their code on
    ``schedule``; ``manual``: only when someone asks on the dashboard. Neither runs on
    each push."""
    schedule: str = "0 */4 * * *"
    """Cron (UTC) for the periodic check."""
    max_prs: int = Field(default=10, ge=1, le=100)
    """At most this many PRs per periodic check, highest priority first."""
    max_comments: int = Field(default=20, ge=1, le=100)
    """At most this many comments one check reads, the oldest first."""

    @field_validator("schedule")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        from croniter import croniter

        if not croniter.is_valid(value):
            raise ValueError(
                f"review.comments.schedule is not a valid cron expression: {value!r}"
            )
        return value


class Review(_Section):
    trigger: Literal["manual", "scan", "events"] = "manual"
    """What starts a review. ``manual``: a reviewer selects PRs (dashboard, ``/am review``,
    chat). ``scan``: a periodic scan on ``scan.schedule``. ``events``: as soon as a PR opens
    or gets new commits."""
    scan: ReviewScan = Field(default_factory=ReviewScan)
    comments: CommentChecks = Field(default_factory=CommentChecks)
    """Whether open review comments are addressed by the code: an agent reads only
    each comment and the code around it (``playbooks/comment_check``)."""
    skill: str = "builtin:pr-review"
    """The skill reviews follow. ``builtin:<name>``: the repository's own review skill
    when it has one (a skill whose name or folder says review), else the service's
    skill ``<name>``. ``service:<name>``: the service's skill ``<name>`` even when the
    repository has its own. ``repo:<path>``: the repository's skill at ``<path>``
    (its ``SKILL.md``), read from the default branch."""
    instructions: str | None = Field(default=None, max_length=8000)
    """How the repository's maintainers want its PRs reviewed, in their own words: what
    to check, what matters most, what to leave alone. Every review, follow-up, and
    revision follows it on top of the skill, and it wins where they disagree."""
    template: list[str] = Field(default_factory=lambda: list(DEFAULT_SECTIONS))
    recommendations: dict[Literal["ready", "changes", "blocked"], str] = Field(
        default_factory=lambda: {
            "ready": "Ready",
            "changes": "Changes requested",
            "blocked": "Blocked",
        }
    )
    verify: bool = True
    cross_agent: bool = False
    max_inline_comments: int = 20
    suggest_labels: bool = True
    """Whether reviews and follow-ups suggest labels from the repository's own (never
    the role labels the service manages); a suggestion is a ``label.apply`` action."""
    max_suggested_labels: int = Field(default=3, ge=1, le=10)
    writing_guide: str | None = Field(default=None, max_length=8000)
    """How every review, follow-up, and revision of one is written, in plain words;
    ``None`` uses the service's default (``playbooks.prompts.DEFAULT_WRITING_GUIDE``)."""
    quick_quality: bool = True
    """Whether every open PR gets a quick quality score from its diff before it is
    reviewed, in the background (``playbooks/quality.py``)."""
    quality_guide: str | None = Field(default=None, max_length=8000)
    """How the reviewing agent judges the PR's quality from 0 to 100; ``None`` uses
    the service's default (``playbooks.prompts.DEFAULT_QUALITY_GUIDE``)."""

    def meaning_of(self, recommendation: str | None) -> str | None:
        """Map a recommendation label back to ``ready``, ``changes``, or ``blocked``."""
        for meaning, label in self.recommendations.items():
            if recommendation and recommendation.strip().lower() in {
                label.lower(),
                meaning,
            }:
                return meaning
        return None


class LabelRule(_Section):
    label: str
    keywords: list[str] = Field(default_factory=list)
    """Regular expressions matched as whole words against the PR title."""
    title_prefixes: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class Labels(_Section):
    roles: dict[str, str] = Field(default_factory=dict)
    rules: list[LabelRule] | str | None = None
    """Inline rules, or a knowledge file path in the repository's memory directory."""
    never_remove_human_labels: bool = True

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(ROLES))
        if unknown:
            raise ValueError(
                f"unknown label roles {unknown}; known roles are {list(ROLES)}"
            )
        return value


class FlakeSignature(_Section):
    id: str
    pattern: str
    category: Literal["flaky", "infra"] = "flaky"
    workflow: str | None = None
    job: str | None = None
    step: str | None = None
    description: str = ""


class Rerun(_Section):
    max_per_sha: int = 2
    backoff_seconds: int = 120


class RunnerGroup(_Section):
    scarce: bool = False
    daily_budget: int | None = None


class CiStart(_Section):
    """How the repository's CI starts and what its jobs mean (design 5.5, merge
    scheduling): written by its maintainers, or drafted by ``playbooks/ci_rules.py``
    and saved by one."""

    how: str = ""
    """In plain words, for the agents that watch CI: what starts it (a push, the gate
    label, a maintainer approving a fork's workflows, a comment) and what to expect."""
    gate_jobs: list[str] = Field(default_factory=list)
    """Jobs that fail on purpose when CI was not asked for (the gate label missing):
    their failure means CI did not start, never that a test failed."""
    summary_jobs: list[str] = Field(default_factory=list)
    """Jobs that only report the other jobs' results: never a failure of their own."""
    optional_checks: list[str] = Field(default_factory=list)
    """Checks a PR may merge without passing, though the branch rules list them (a
    sign-off check that only warns): the merge gate does not wait on them, and they
    are never a failure to look into or fix."""
    start_minutes: int = 5
    """How long a scheduled PR's CI may take to start before an agent looks into why."""
    run_minutes: int = 180
    """How long it may run before an agent looks into why."""


class Ci(_Section):
    provider: str = "github-actions"
    required_workflows: list[str] = Field(default_factory=list)
    cancelled_is_failure: bool = False
    rerun: Rerun = Field(default_factory=Rerun)
    runner_groups: dict[str, RunnerGroup] = Field(default_factory=dict)
    flake_signatures: list[FlakeSignature] = Field(default_factory=list)
    gate_daily_budget: int | None = None
    root_cause: bool = True
    """Look for the root cause of failures that are not flakes, in a checkout of the
    PR head (design 5.5); ``false`` keeps the one-line explanation from the log."""
    start: CiStart = Field(default_factory=CiStart)


class Arming(_Section):
    who: Literal["write", "maintain", "admin", "codeowners"] = "write"


class MergeMessage(_Section):
    subject: str = "{title} (#{number})"
    preserve_trailers: list[str] = Field(default_factory=list)


class Freeze(_Section):
    start: datetime
    end: datetime
    reason: str = ""


ReadyCondition = Literal[
    "approvals",
    "threads_resolved",
    "no_changes_requested",
    "not_draft",
    "no_blocking_labels",
    "no_conflict",
    "hygiene",
]
READY_CONDITIONS: tuple[ReadyCondition, ...] = (
    "approvals",
    "threads_resolved",
    "no_changes_requested",
    "not_draft",
    "no_blocking_labels",
    "no_conflict",
    "hygiene",
)
"""What a PR may have to meet before it is schedulable, named as the merge gate's
items (``hygiene``: every blocking check). Being up to date and CI are never among
them: the schedule merges the base in and runs CI itself."""


class MergeSchedule(_Section):
    """The repository's merge scheduling (design 5.5)."""

    ready: list[ReadyCondition] | None = None
    """What a PR must meet before it is schedulable (the repository's target for
    running its final CI and merging); ``None`` for every condition its merge rules
    hold a PR to (``READY_CONDITIONS``). What it leaves out but a merge needs is
    waited for after CI. Drafted by ``playbooks/ci_rules.py``, saved by a
    maintainer."""

    auto: bool = False
    """Schedule every PR that becomes ready, as ``auto_by``; otherwise a maintainer
    schedules each one."""
    auto_by: str | None = None
    """The maintainer who turned automatic scheduling on: what it schedules is armed
    as theirs, and only while they still maintain the repository."""
    parallel: int = Field(default=2, ge=1, le=20)
    """How many scheduled PRs run their CI at once when PRs need not be up to date
    with the base; when they must be, one at a time."""
    notes: str = ""
    """What the repository asks of a PR before it merges, in plain words, beside the
    rules its forge enforces (the contribution guide's): shown on the Merge
    scheduling page."""


class Merge(_Section):
    enabled: bool = True
    """Merge scheduling (design 5.5), on by default: it merges nothing that a
    maintainer did not schedule, and automatic scheduling stays off until one turns
    it on (``schedule.auto``)."""
    method: Literal["squash", "merge", "rebase"] = "squash"
    arming: Arming = Field(default_factory=Arming)
    approvals: int = 1
    blocking_roles: list[str] = Field(default_factory=lambda: ["changes_requested"])
    message: MergeMessage = Field(default_factory=MergeMessage)
    update_branch: bool = True
    freezes: list[Freeze] = Field(default_factory=list)
    schedule: MergeSchedule = Field(default_factory=MergeSchedule)


class Priority(_Section):
    enabled: bool = True
    rubric: Rubric | str | None = None
    """Inline rubric or a path in the repository's memory directory; ``None`` uses the default."""
    stage_goals: str | None = "rubric/stage_goals.yaml"


class Timing(_Section):
    calendar: str | None = None
    holidays: list[date] = Field(default_factory=list)
    workdays: list[date] = Field(default_factory=list)
    """Weekend days that are working days."""
    weekend: list[int] = Field(default_factory=lambda: [5, 6])
    first_response_days: int = 2
    rereview_days: int = 3
    stale_days: int = 30


class LabTemplate(_Section):
    run: str
    cpu_fallback: str | None = None
    needs_gpu: bool = False
    timeout_seconds: int = 3600


class LabGuard(_Section):
    forbid: list[str] = Field(default_factory=list)
    """Regular expressions a lab command must not match."""
    ray_private_instance: bool = False


class LabBinding(_Section):
    """How this repository uses a lab or test machine. A repository uses only the
    machines its profile lists with ``use: true``; the machine itself (how to reach
    it, its limits) belongs to the service settings or the host config."""

    use: bool = True
    timeout_seconds: int | None = Field(default=None, ge=60, le=6 * 3600)
    """The longest a reproduction may run for this repository; the machine's own
    limit still applies."""
    base_clone: str | None = None
    worktree_root: str | None = None
    env_roots: list[str] = Field(default_factory=list)
    throwaway_env_root: str | None = None
    protected_paths: list[str] = Field(default_factory=list)
    setup: str | None = None
    """Shell run before each template and each reproduction, after the machine's own
    setup, such as activating this repository's environment."""
    guard: LabGuard = Field(default_factory=LabGuard)
    templates: dict[str, LabTemplate] = Field(default_factory=dict)


class Languages(_Section):
    public: str = "en"
    internal: str = "en"
    style_guide: str | None = None
    style_phrases: list[str] = Field(
        default_factory=lambda: [
            "it is worth noting",
            "seamlessly",
            "leverage",
            "robust",
            "in order to",
            "powerful",
        ]
    )


class Confidentiality(_Section):
    internal_entities: list[str] | str = Field(default_factory=list)
    """Names that must not appear in public text, inline or as a knowledge file path."""
    internal_terms: list[str] = Field(
        default_factory=lambda: ["priority score", "rubric", "stage goal"]
    )


class Conditions(_Section):
    author: Literal["any", "team", "external"] = "any"
    recommendation: Literal["ready", "changes", "blocked"] | None = None
    paths: list[str] = Field(default_factory=list)
    """When set, every changed file must match one of these globs."""


class PolicyRule(_Section):
    level: Autonomy = Autonomy.SHADOW
    conditions: Conditions = Field(default_factory=Conditions)
    otherwise: Autonomy = Autonomy.SHADOW
    """The level when the conditions do not hold."""


class Comments(_Section):
    command_prefix: str = "/am"
    footer: str = "Drafted by auto-maintainer with {runtime}{approval}."
    legacy_markers: list[str] = Field(default_factory=list)
    """Regular expressions whose first group is a reviewed SHA."""


class Route(_Section):
    primary: str
    fallback: list[str] = Field(default_factory=list)
    verifier: str | None = None
    model: str | None = None
    effort: str | None = None
    transport: Literal["exec", "acp"] = "exec"
    timeout_seconds: int = 1800
    permissions: Literal["restricted", "auto", "bypass"] = "auto"
    """``auto`` (the default) runs each CLI in its own auto mode and sends what it
    would ask a person to the dashboard for a reviewer. ``restricted`` denies those
    requests without asking. ``bypass`` skips every permission check and sandbox; it
    applies only when the operator set ``allow_bypass`` on the runtime and, for input
    from outside the team, ``bypass_untrusted`` is set too."""
    bypass_untrusted: bool = False
    """With ``True``, PRs from outside the team may run in bypass mode as well."""
    question_timeout_seconds: int = Field(default=1800, ge=30)
    """How long an agent's question waits for a reviewer before it counts as denied."""


class PlaybookToggle(_Section):
    enabled: bool = True


class MemorySettings(_Section):
    knowledge_auto_merge: bool = False
    use_global: bool = False
    notice_hours: int = 24


class RepoProfile(BaseModel):
    """The approved settings of one repository."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(PROFILE_SCHEMA, alias="schema")
    repo: str
    extends: list[str] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)
    people: People = Field(default_factory=People)
    access: Access = Field(default_factory=Access)
    contribution: Contribution = Field(default_factory=Contribution)
    checks: list[CheckSpec] = Field(default_factory=list)
    review: Review = Field(default_factory=Review)
    labels: Labels = Field(default_factory=Labels)
    ci: Ci = Field(default_factory=Ci)
    merge: Merge = Field(default_factory=Merge)
    priority: Priority = Field(default_factory=Priority)
    timing: Timing = Field(default_factory=Timing)
    labs: dict[str, LabBinding] = Field(default_factory=dict)
    languages: Languages = Field(default_factory=Languages)
    confidentiality: Confidentiality = Field(default_factory=Confidentiality)
    policy: dict[ActionKind, PolicyRule] = Field(default_factory=dict)
    ceilings: dict[ActionKind, Autonomy] = Field(default_factory=dict)
    comments: Comments = Field(default_factory=Comments)
    routing: dict[str, Route] = Field(default_factory=dict)
    notify: dict[str, str] = Field(default_factory=dict)
    playbooks: dict[str, PlaybookToggle] = Field(default_factory=dict)
    memory: MemorySettings = Field(default_factory=MemorySettings)

    @field_validator("repo")
    @classmethod
    def _repo_name(cls, value: str) -> str:
        return check_repo(value)

    @field_validator("schema_version")
    @classmethod
    def _schema_known(cls, value: int) -> int:
        if value != PROFILE_SCHEMA:
            raise ValueError(
                f"profile schema {value} is not supported; expected {PROFILE_SCHEMA}"
            )
        return value

    def rule(self, kind: ActionKind) -> PolicyRule:
        """The policy rule for ``kind``, capped by its ceiling."""
        rule = self.policy.get(kind, PolicyRule())
        ceiling = self.ceilings.get(kind)
        if ceiling is None:
            return rule
        return rule.model_copy(
            update={
                "level": rule.level.cap(ceiling),
                "otherwise": rule.otherwise.cap(ceiling),
            }
        )

    def label_for(self, role: str) -> str | None:
        return self.labels.roles.get(role)

    def playbook_enabled(self, name: str) -> bool:
        toggle = self.playbooks.get(name)
        return True if toggle is None else toggle.enabled

    def route(self, task_kind: str, defaults: dict[str, Route]) -> Route | None:
        return self.routing.get(task_kind) or defaults.get(task_kind)

    def dump(self) -> dict[str, Any]:
        """The profile as plain data, with the ``schema`` alias."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
