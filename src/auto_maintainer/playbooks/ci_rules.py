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


"""A repository's merge scheduling rules, drafted by an agent for its maintainers
(design 5.5): every setting of its merge schedule but turning it and automatic
scheduling on, which stay a maintainer's own choice. What makes a PR schedulable,
which labels hold a merge back, which required checks may stay unpassed, how it
merges (method, message, trailers, merging the base in first, how many at once),
how its CI starts and what its jobs mean, and how failed CI is judged and run again.

Two ways fill a draft, both on a maintainer's request from the Merge scheduling
page. **Investigate**: an agent reads the default branch's workflows and
contribution guide in a read-only checkout, beside facts the service gathers (the
branch rules, the fork-approval setting, which jobs fail at the open PRs' heads).
**Describe**: a quick agent call turns the maintainer's own words into the fields,
changing only what they spoke to. Either way the draft keeps only what the
repository has (``checked``: conditions it knows, label roles the profile names,
jobs a workflow declares or a PR ran, required checks, patterns that compile), is
kept for the page (``DRAFT_KEY``), and becomes the profile's only when a maintainer
saves it, through the configuration gate.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import READY_CONDITIONS, RepoProfile, ResolveKind
from ..domain.run_ci import latest_checks
from ..domain.snapshot import RepoInfo
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import schema_hint, untrusted

COMMAND = "ci_rules"
DRAFT_KEY = "ci-rules:{repo}"
MAX_PATTERNS = 30

FIELDS = """Fields:
- `ready`: what a PR must meet before it is scheduled for its final CI run and merge, as a subset of `approvals` (the approvals the branch rules require, code owners' included), `threads_resolved` (every review conversation resolved), `no_changes_requested`, `not_draft`, `no_blocking_labels`, `no_conflict` (merges with its base without conflicts), `hygiene` (the repository's own PR checks: title, sign-off). Being up to date with the base and CI are never part of it: the service merges the base in and runs CI after scheduling.
- `blocking_roles`: the label roles (from the list below) whose label holds a PR's merge back while it is on the PR; empty when no label does.
- `optional_checks`: required checks the repository merges without passing, such as a sign-off check that only warns; only names from the required checks below.
- `how`: a few plain sentences a maintainer and an agent watching CI both need: what starts a PR's CI, which events start a run again, how runs replace or cancel each other, and anything that keeps the full CI from running.
- `gate_label`: the label whose absence keeps the full CI from running; empty when there is none.
- `gate_jobs`: jobs that fail on purpose when CI was not asked for (the gate label missing, a draft PR). `summary_jobs`: jobs that only collect the other jobs' results.
- `start_minutes`: how long CI may take to start before someone looks into it; `run_minutes`: how long it may run.
- `diagnose`: how a failed job is judged a flake: `agent` (a quick agent look at its log when no flake pattern matches) or `patterns` (only the flake patterns; no agent reads a log before the root cause).
- `max_reruns`: how many times a run that flaked is run again at one commit; `backoff_seconds`: how long between reruns; `rerun_unknown`: how many times a failure nothing calls a flake is run again before it counts as real (0: never).
- `root_cause`: whether an agent looks for the root cause of a real failure in a checkout of the PR.
- `flake_patterns`: known flakes, each an `id`, a Python regular expression `pattern` matched against a failed job's log, a `category` (`flaky`, or `infra` for the runner's own failures), and a `description`.
- `merge_notes`: what the repository asks of a PR before it merges beyond the forge's rules, in plain words; `approvals`: the approvals it asks for when that is more than the forge's rules require, else 0.
- `merge_method`: how PRs merge (`squash`, `merge`, or `rebase`), one the repository allows; `subject`: the merge commit's subject, a template with `{title}` and `{number}`; `preserve_trailers`: commit trailers a squash keeps from the PR's commits (such as `Signed-off-by` where commits must be signed off, `Co-authored-by`).
- `update_branch`: whether a PR merges its base in before it merges even where the branch rules do not require it to be up to date (one at a time then).
- `parallel`: how many scheduled PRs may run their CI at once when PRs need not be up to date (1 to 20; fewer where CI runs on scarce machines such as GPUs).
- `required_workflows`: workflows, by name, whose runs must pass before a merge besides the checks the branch rules require; `cancelled_is_failure`: whether a cancelled job counts as a failure rather than as a run that stopped short.
- `resolve_conflicts`: whether the schedule resolves a scheduled PR's minor conflicts with its base by itself (conflicts that change no code's meaning); `resolve_kinds`: which, each a `name` (lowercase, `-` or `_`), `paths` (glob patterns of the files: `*` within a folder, `**` across folders), a `description` (which conflicts in those files count, and how to resolve them), and `enabled`. By default docs, README files, and lists of install targets or dependencies; name the repository's own files where it keeps such lists (its install scripts, requirement files).
- `signoff_from_authors`: whether a squash signs off in the name of the PR's commit authors when none of its commits is signed off, for a repository whose default branch checks for a sign-off.
- `arming_who`: who may schedule a PR to merge besides the repository's maintainers' own rules: `write`, `maintain`, `admin`, or `codeowners`."""

INVESTIGATE = """You are finding out how {repo} wants its pull requests merged and its CI handled, for a service that runs the final CI of the PRs its maintainers schedule and merges those that pass. The working directory is a read-only checkout of the default branch `{branch}`. Read `.github/workflows/` (the CI workflows, and the workflows they call) and the contribution guide (`CONTRIBUTING*`, `docs/` pages about contributing, reviewing, or CI), then fill in the fields below from what they say, keeping each current value (given below) where they say nothing. Do not change files, run anything, or fetch anything.

{fields}"""

DESCRIBE = """A maintainer of {repo} told you, in their own words, how the service should schedule the repository's pull requests to merge and handle their CI. Turn what they said into the fields below. Start from the current settings given after their words and change only what they spoke to; keep every other field exactly as it is now. When they name something the repository does not have (a label, a check, a job), leave it out. Judge only from what is given here: do not run commands, read files, or fetch anything.

{fields}"""


class FlakePattern(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=500)
    category: Literal["flaky", "infra"] = "flaky"
    description: str = Field(default="", max_length=500)


class ResolveKindDraft(BaseModel):
    """A kind of conflict the schedule resolves, as drafted; ``checked`` keeps only
    what is a valid ``ResolveKind``."""

    name: str = Field(max_length=80)
    paths: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True


class CiRulesDraft(BaseModel):
    """What the agent drafted; checked before anyone sees it."""

    how: str = Field(default="", description="How the CI starts, in plain sentences.")
    gate_label: str = Field(default="", description="The gate label, or empty.")
    gate_jobs: list[str] = Field(default_factory=list)
    summary_jobs: list[str] = Field(default_factory=list)
    start_minutes: int = Field(default=5, ge=1, le=24 * 60)
    run_minutes: int = Field(default=180, ge=5, le=7 * 24 * 60)
    ready: list[str] = Field(default_factory=list)
    blocking_roles: list[str] = Field(default_factory=list)
    optional_checks: list[str] = Field(default_factory=list)
    diagnose: Literal["agent", "patterns"] = "agent"
    max_reruns: int = Field(default=2, ge=0, le=10)
    backoff_seconds: int = Field(default=120, ge=0, le=24 * 3600)
    rerun_unknown: int = Field(default=0, ge=0, le=5)
    root_cause: bool = True
    flake_patterns: list[FlakePattern] = Field(default_factory=list)
    merge_notes: str = Field(default="")
    approvals: int = Field(default=0, ge=0, le=20)
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    subject: str = Field(default="{title} (#{number})", max_length=200)
    preserve_trailers: list[str] = Field(default_factory=list)
    update_branch: bool = True
    parallel: int = Field(default=2, ge=1, le=20)
    required_workflows: list[str] = Field(default_factory=list)
    cancelled_is_failure: bool = False
    resolve_conflicts: bool = True
    resolve_kinds: list[ResolveKindDraft] = Field(default_factory=list)
    signoff_from_authors: bool = False
    arming_who: Literal["write", "maintain", "admin", "codeowners"] = "write"


def current(profile: RepoProfile) -> CiRulesDraft:
    """The repository's settings now, in the draft's terms."""
    start, rerun = profile.ci.start, profile.ci.rerun
    ready = profile.merge.schedule.ready
    return CiRulesDraft(
        how=start.how,
        gate_label=profile.label_for("ci_gate") or "",
        gate_jobs=start.gate_jobs,
        summary_jobs=start.summary_jobs,
        start_minutes=start.start_minutes,
        run_minutes=start.run_minutes,
        ready=list(ready) if ready is not None else list(READY_CONDITIONS),
        blocking_roles=list(profile.merge.blocking_roles),
        optional_checks=start.optional_checks,
        diagnose=profile.ci.diagnose,
        max_reruns=rerun.max_per_sha,
        backoff_seconds=rerun.backoff_seconds,
        rerun_unknown=rerun.unknown,
        root_cause=profile.ci.root_cause,
        flake_patterns=[
            FlakePattern(
                id=s.id,
                pattern=s.pattern,
                category=s.category,
                description=s.description,
            )
            for s in profile.ci.flake_signatures
        ],
        merge_notes=profile.merge.schedule.notes,
        approvals=0,
        merge_method=profile.merge.method,
        subject=profile.merge.message.subject,
        preserve_trailers=list(profile.merge.message.preserve_trailers),
        update_branch=profile.merge.update_branch,
        parallel=profile.merge.schedule.parallel,
        required_workflows=list(profile.ci.required_workflows),
        cancelled_is_failure=profile.ci.cancelled_is_failure,
        resolve_conflicts=profile.merge.schedule.resolve.enabled,
        resolve_kinds=[
            ResolveKindDraft(**k.model_dump())
            for k in profile.merge.schedule.resolve.kinds
        ],
        signoff_from_authors=profile.merge.message.signoff_from_authors,
        arming_who=profile.merge.arming.who,
    )


def _workflow_text(root: Path) -> str:
    folder = root / ".github" / "workflows"
    if not folder.is_dir():
        return ""
    parts = []
    for path in sorted(folder.iterdir()):
        if (
            path.suffix in (".yml", ".yaml")
            and path.is_file()
            and not path.is_symlink()
        ):
            parts.append(path.read_text(errors="replace"))
    return "\n".join(parts)


def known_workflows(text: str) -> set[str]:
    """The workflow names the workflow files declare (their top-level ``name``)."""
    return set(
        re.findall(r"^name:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$", text, re.MULTILINE)
    )


TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,40}$")


def known_jobs(text: str) -> set[str]:
    """The job ids and job names the workflow files declare."""
    ids = set(re.findall(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", text, re.MULTILINE))
    names = set(
        re.findall(r"^    name:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$", text, re.MULTILINE)
    )
    return ids | names


def checked(
    draft: CiRulesDraft,
    workflows: str,
    labels: list[str],
    required: list[str] | None = None,
    roles: dict[str, str] | None = None,
    jobs: set[str] | None = None,
    methods: list[str] | None = None,
    workflow_names: set[str] | None = None,
) -> tuple[CiRulesDraft, list[str]]:
    """``draft`` with what the repository does not have dropped, and what was:
    conditions outside ``READY_CONDITIONS``, label roles the profile does not name,
    optional checks the branch rules do not require, jobs no workflow declares (nor
    ``jobs``, the names already known), a label it lacks, and flake patterns that
    do not compile."""
    known = known_jobs(workflows) | (jobs or set())
    dropped: list[str] = []
    ready = [c for c in dict.fromkeys(draft.ready) if c in READY_CONDITIONS]
    dropped += [
        f"condition {c!r}, which is not one" for c in draft.ready if c not in ready
    ]
    named = roles or {}
    blocking = [r for r in dict.fromkeys(draft.blocking_roles) if r in named]
    dropped += [
        f"label role {r!r}, which the profile gives no label"
        for r in draft.blocking_roles
        if r not in blocking
    ]
    names = set(required or [])
    optional = [c for c in dict.fromkeys(draft.optional_checks) if c in names]
    dropped += [
        f"optional check {c!r}, which the branch rules do not require"
        for c in draft.optional_checks
        if c not in optional
    ]
    keep: dict[str, list[str]] = {}
    for field in ("gate_jobs", "summary_jobs"):
        kept = []
        for name in getattr(draft, field):
            if name in known:
                kept.append(name)
            else:
                dropped.append(f"job {name!r}, which no workflow declares")
        keep[field] = sorted(set(kept))
    label = draft.gate_label.strip()
    if label and label not in labels:
        dropped.append(f"label {label!r}, which the repository does not have")
        label = ""
    patterns: list[FlakePattern] = []
    for p in draft.flake_patterns:
        try:
            re.compile(p.pattern)
        except re.error as e:
            dropped.append(f"flake pattern {p.id!r}, which does not compile: {e}")
            continue
        if p.id in {q.id for q in patterns}:
            dropped.append(f"flake pattern {p.id!r}, named twice")
            continue
        patterns.append(p)
    if len(patterns) > MAX_PATTERNS:
        dropped.append(f"flake patterns past the first {MAX_PATTERNS}")
        patterns = patterns[:MAX_PATTERNS]
    method = draft.merge_method
    if methods and method not in methods:
        dropped.append(f"merge method {method!r}, which the repository does not allow")
        method = "squash" if "squash" in methods else methods[0]  # type: ignore[assignment]
    subject = draft.subject.strip() or "{title} (#{number})"
    try:
        subject.format(title="t", number=1)
    except (KeyError, IndexError, ValueError):
        dropped.append(f"subject {subject!r}, which names more than title and number")
        subject = "{title} (#{number})"
    trailers = [t for t in dict.fromkeys(draft.preserve_trailers) if TRAILER.match(t)]
    dropped += [
        f"trailer {t!r}, which is not one"
        for t in draft.preserve_trailers
        if t not in trailers
    ]
    names = known_workflows(workflows) | (workflow_names or set())
    required_workflows = [
        w for w in dict.fromkeys(draft.required_workflows) if w in names
    ]
    dropped += [
        f"workflow {w!r}, which no workflow file names"
        for w in draft.required_workflows
        if w not in required_workflows
    ]
    kinds: list[ResolveKindDraft] = []
    for k in draft.resolve_kinds:
        try:
            valid = ResolveKind.model_validate(k.model_dump())
        except ValidationError:
            dropped.append(f"conflict kind {k.name!r}, which is not a valid one")
            continue
        if valid.name in {x.name for x in kinds}:
            dropped.append(f"conflict kind {k.name!r}, named twice")
            continue
        kinds.append(ResolveKindDraft(**valid.model_dump()))
    return draft.model_copy(
        update={
            **keep,
            "resolve_kinds": kinds,
            "merge_method": method,
            "subject": subject,
            "preserve_trailers": trailers,
            "required_workflows": required_workflows,
            "ready": ready,
            "blocking_roles": blocking,
            "optional_checks": optional,
            "gate_label": label,
            "flake_patterns": patterns,
        }
    ), dropped


def _facts(
    info: RepoInfo, profile: RepoProfile, failing: Counter[str], prs: int
) -> str:
    return "\n".join(
        [
            f"Default branch: {info.default_branch}",
            f"Required approvals: {info.required_approvals}",
            f"A code owner must approve: {info.requires_code_owner_reviews}",
            f"Conversations must be resolved: {info.requires_thread_resolution}",
            f"PRs must be up to date with the base: {info.requires_up_to_date}",
            f"Required checks: {', '.join(info.required_checks or []) or '(none)'}",
            f"Merge methods allowed: {', '.join(info.merge_methods or []) or 'unknown'}",
            f"Fork workflows wait for approval from: {info.fork_approval or 'unknown'}",
            "Label roles (role: label): "
            + (
                ", ".join(
                    f"{r}: {lab}" for r, lab in sorted(profile.labels.roles.items())
                )
                or "(none)"
            ),
            f"Labels: {', '.join(info.labels[:200])}",
            f"Jobs failing at the heads of the {prs} open PRs: "
            + (", ".join(f"{n} ({k})" for n, k in failing.most_common(15)) or "(none)"),
        ]
    )


class CiRulesPlaybook(Playbook):
    name = "ci_rules"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.payload.get("command") == COMMAND and bool(event.payload.get("by"))

    def key(self, event: Event) -> str:
        return f"ci_rules:{event.repo}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo
        repo = event.repo
        by = str(event.payload.get("by"))
        words = str(event.payload.get("text") or "").strip()
        ctx.requested_by = by
        ctx.trusted_input = True
        ctx.agent = event.payload.get("agent") or None
        info = await ctx.forge.get_repo(repo)
        failing: Counter[str] = Counter()
        seen_jobs: set[str] = set()
        seen_workflows: set[str] = set(profile.ci.required_workflows)
        prs = 0
        for pr in await ctx.store.list_snapshots(repo):
            prs += 1
            for c in latest_checks(pr.checks):
                seen_jobs |= {c.name, c.name.split(" / ")[-1]}
                if c.workflow:
                    seen_workflows.add(c.workflow)
                if c.failed:
                    failing[c.name] += 1
        now = current(profile)
        known = seen_jobs | set(now.gate_jobs) | set(now.summary_jobs)
        settings = "Current settings:\n" + json.dumps(
            now.model_dump(mode="json"), ensure_ascii=False, indent=1
        )
        facts = "What the service read from the forge:\n" + _facts(
            info, profile, failing, prs
        )
        task: dict = {
            "output_schema": CiRulesDraft.model_json_schema(),
            "repo": repo,
            "requested_by": by,
            "activity": "ci_rules",
        }
        if words:
            prompt = "\n\n".join(
                [
                    DESCRIBE.format(repo=repo, fields=FIELDS),
                    "Their words:\n" + untrusted("maintainer.words", words, 8_000),
                    settings,
                    facts,
                    schema_hint(CiRulesDraft.model_json_schema()),
                ]
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.ONBOARD,
                    prompt=prompt,
                    workdir=ctx.scratch(),
                    mode="read_only",
                    budget="quick",
                    **task,  # type: ignore[arg-type]
                ),
                CiRulesDraft,
            )
            workflows = ""
        else:
            workdir = await ctx.workspace.prepare(
                repo,
                None,
                info.default_branch_sha or info.default_branch,
                info.default_branch,
            )
            try:
                prompt = "\n\n".join(
                    [
                        INVESTIGATE.format(
                            repo=repo, branch=info.default_branch, fields=FIELDS
                        ),
                        settings,
                        facts,
                        schema_hint(CiRulesDraft.model_json_schema()),
                    ]
                )
                answer = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.ONBOARD,
                        prompt=prompt,
                        workdir=workdir,
                        mode="read_only",
                        **task,  # type: ignore[arg-type]
                    ),
                    CiRulesDraft,
                )
                workflows = _workflow_text(Path(workdir))
            finally:
                await ctx.workspace.release(workdir)
        if not answer.ok or answer.structured is None:
            if may_succeed_later(answer):
                return PlaybookResult(retry_after=300)
            return PlaybookResult(failed=answer.error or "no usable answer")
        draft, dropped = checked(
            CiRulesDraft.model_validate(answer.structured),
            workflows,
            info.labels,
            info.required_checks,
            dict(profile.labels.roles),
            known,
            info.merge_methods,
            seen_workflows,
        )
        await ctx.store.set_state(
            DRAFT_KEY.format(repo=repo),
            {
                "draft": draft.model_dump(mode="json"),
                "dropped": dropped,
                "by": by,
                "at": ctx.clock.now().isoformat(),
                "sha": info.default_branch_sha,
                "source": "words" if words else "investigation",
                "words": words,
            },
        )
        return PlaybookResult(
            result={"drafted": True, "dropped": dropped, "how": draft.how[:300]}
        )
