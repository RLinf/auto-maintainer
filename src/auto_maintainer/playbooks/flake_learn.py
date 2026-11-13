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


"""Learning a flake pattern from a person's verdict (design 5.5).

When someone marks a failed CI job flaky, a quick agent reads that job's log and
writes one pattern that names the failure, so the same flake is recognized in other
runs with no agent at all. The pattern is kept only when it compiles, matches the
log it came from, matches no ordinary test or build failure (``GENERIC``), and is
not one the repository knows already; it then waits on the Merge scheduling page as
a suggestion (``SUGGESTIONS``) until a maintainer adds it to the repository's flake
patterns, through the gate.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.profile import RepoProfile
from ..domain.snapshot import CheckRun
from .base import Playbook, PlaybookContext, PlaybookResult
from .ci_doctor import excerpt, match_signature
from .prompts import schema_hint, untrusted

COMMAND = "flake_pattern"
SUGGESTIONS = "flake-suggestions:{repo}"
KEPT = 10

LEARN = """A maintainer of {repo} marked a failed CI job flaky: it failed for a reason outside the change and passes when run again. Write one Python regular expression that matches this failure's distinctive error lines in its log, so the same flake is recognized in other runs and other PRs, and that would not match an ordinary test failure or build error. Use the stable part of the message (the exception and its text, the service that timed out) and leave out times, paths, ids, and numbers. Give it a short kebab-case `id`, a `category` (`flaky`, or `infra` when the runner itself failed: disk, network to the runner, a lost runner), and a one-line `description`. Judge only from the log given here: do not run commands, read files, or fetch anything."""

GENERIC = (
    "FAILED tests/test_loader.py::test_empty_batch - AssertionError: assert 1 == 2",
    "Error: Process completed with exit code 1.",
    "error: command failed with exit status 2",
    "E   TypeError: 'NoneType' object is not iterable",
    "make: *** [Makefile:12: test] Error 1",
)
"""Lines of ordinary failures a flake pattern must not match."""


class Suggested(BaseModel):
    id: str = Field(min_length=2, max_length=60)
    pattern: str = Field(min_length=6, max_length=500)
    category: str = "flaky"
    description: str = Field(default="", max_length=300)


def usable(found: Suggested, log: str) -> str | None:
    """Why ``found`` is not a pattern to suggest, or ``None`` when it is."""
    try:
        rx = re.compile(found.pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        return f"it does not compile: {e}"
    if not rx.search(log):
        return "it does not match the log it came from"
    hits = [g for g in GENERIC if rx.search(g)]
    if hits:
        return f"it matches an ordinary failure too: {hits[0]!r}"
    if found.category not in ("flaky", "infra"):
        return f"its category {found.category!r} is neither flaky nor infra"
    return None


class FlakeLearn(Playbook):
    name = "flake_learn"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return (
            event.payload.get("command") == COMMAND
            and bool(event.payload.get("by"))
            and bool(event.payload.get("observation"))
        )

    def key(self, event: Event) -> str:
        return f"flake_learn:{event.repo}:{event.payload.get('observation')}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo
        repo = event.repo
        ctx.requested_by = str(event.payload.get("by"))
        obs = next(
            (
                o
                for o in await ctx.store.list_ci_observations(repo)
                if o.id == event.payload.get("observation")
            ),
            None,
        )
        if obs is None or obs.verdict != "flaky":
            return PlaybookResult(result={"skipped": "no flaky verdict to learn from"})
        check = CheckRun(
            id=obs.check_id,
            name=obs.check_name,
            workflow=obs.workflow,
            run_id=obs.run_id,
            details_url=obs.details_url,
        )
        # A job's log, or the build log of an outside service's check (Read the Docs).
        log = await ctx.ci.log_of(repo, check) or ""
        if not log.strip():
            return PlaybookResult(result={"skipped": "the job's log is gone"})
        known = await ctx.knowledge.flake_signatures(repo, profile)
        if match_signature(log, check, known) is not None:
            return PlaybookResult(result={"skipped": "a known pattern matches it"})
        prompt = "\n\n".join(
            [
                LEARN.format(repo=repo),
                f"Job: {obs.check_name} (workflow {obs.workflow})",
                untrusted("ci.log", excerpt(log), 30_000),
                schema_hint(Suggested.model_json_schema()),
            ]
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.CI_DIAGNOSE,
                prompt=prompt,
                workdir=ctx.scratch(),
                mode="read_only",
                budget="quick",
                output_schema=Suggested.model_json_schema(),
                repo=repo,
                requested_by=ctx.requested_by,
                untrusted_input=True,
                activity="flake_learn",
            ),
            Suggested,
        )
        if not answer.ok or answer.structured is None:
            return PlaybookResult(result={"skipped": answer.error or "no answer"})
        found = Suggested.model_validate(answer.structured)
        why = usable(found, log)
        if why:
            return PlaybookResult(result={"skipped": f"the pattern {why}"})
        key = SUGGESTIONS.format(repo=repo)
        kept = await ctx.store.get_state(key) or []
        kept = [
            {
                **found.model_dump(mode="json"),
                "from": obs.check_name,
                "observation": obs.id,
                "by": ctx.requested_by,
                "at": ctx.clock.now().isoformat(),
            },
            *(k for k in kept if k.get("id") != found.id),
        ][:KEPT]
        await ctx.store.set_state(key, kept)
        return PlaybookResult(result={"suggested": found.id})
