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

"""Which test machine a PR's review or reproduction runs on (design 5.10).

The candidates are the machines the work may use, in order: the asker's own, then
those the repository uses (``domain/labs.py:offered``). With one candidate, or when
none says what it is for, the first is the answer. Otherwise a short agent task reads
what each machine is suited for (``LabMachine.suited_for``, as its maintainers wrote
it) beside the PR (its title, description, and changed files) and what the run needs
(a reproduction's brief), and picks one: a machine with a robot attached for a PR
that changes that robot's driver. The answer is kept per PR, head, and candidates, so
a review and the reproductions after it ask once.
"""

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, Field

from ..domain.agents import AgentTask, TaskKind
from ..domain.labs import offered, unusable
from ..domain.profile import RepoProfile
from ..domain.snapshot import PrSnapshot
from .base import PlaybookContext
from .prompts import schema_hint, untrusted

log = logging.getLogger(__name__)

CHOICE_STATE = "machine_choice:{repo}#{number}"

CHOOSE = """Choose the test machine to run pull request #{number} in {repo} on. Each machine below says what it is suited for, as its maintainers wrote it. Pick the one whose hardware and purpose fit what this PR changes{need}. When several fit equally, or none says anything that fits, pick the first. Judge only from what is given here: do not run commands, read files, or fetch anything."""


class MachineChoice(BaseModel):
    machine: str = Field(description="The name of the machine, exactly as listed.")
    why: str = Field(default="", description="One line on why it fits.")


def candidates(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, by: str | None
) -> list[str]:
    """The machines the work ``by`` asks for on ``pr`` may use, their own first."""
    machines = ctx.service.machines
    return [
        name
        for name in offered(machines, pr.repo, by)
        if unusable(pr.repo, name, machines.get(name), profile.labs.get(name), by)
        is None
    ]


async def choose_machine(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    profile: RepoProfile,
    by: str | None,
    need: str = "",
) -> str | None:
    """The machine for the work ``by`` asks for on ``pr`` (``need``: what the run
    needs, such as a reproduction's brief); ``None`` when none may take it."""
    names = candidates(ctx, pr, profile, by)
    machines = ctx.service.machines
    if len(names) <= 1 or not any(machines[n].suited_for.strip() for n in names):
        return names[0] if names else None
    key = hashlib.sha256(
        json.dumps(
            [pr.head_sha, names, [machines[n].suited_for for n in names], need]
        ).encode()
    ).hexdigest()[:16]
    state = CHOICE_STATE.format(repo=pr.repo, number=pr.number)
    kept = await ctx.store.get_state(state) or {}
    if (
        isinstance(kept, dict)
        and kept.get("key") == key
        and kept.get("machine") in names
    ):
        return str(kept["machine"])
    listing = "\n".join(
        f"- {n}: {machines[n].suited_for.strip() or 'general use'}"
        + (
            f" (about it: {machines[n].description.strip()[:400]})"
            if machines[n].description.strip()
            else ""
        )
        for n in names
    )
    files = "\n".join(f.path for f in pr.files[:150])
    prompt = "\n\n".join(
        part
        for part in [
            CHOOSE.format(
                number=pr.number,
                repo=pr.repo,
                need=" and what the run needs" if need.strip() else "",
            ),
            "Machines, in order:\n" + listing,
            "What the run needs:\n" + untrusted("run.need", need, 6_000)
            if need.strip()
            else "",
            untrusted("pr.title", pr.title, 500),
            untrusted("pr.body", pr.body or "(empty)", 4_000),
            untrusted("pr.files", files, 8_000),
            schema_hint(MachineChoice.model_json_schema()),
        ]
        if part
    )
    chosen = names[0]
    try:
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.TRIAGE,
                prompt=prompt,
                workdir=ctx.scratch(),
                mode="read_only",
                budget="quick",
                output_schema=MachineChoice.model_json_schema(),
                repo=pr.repo,
                pr=pr.number,
                requested_by=by,
                untrusted_input=True,
                activity="machine_choice",
            ),
            MachineChoice,
        )
    except Exception:  # an answer that fails costs the work only the choice
        log.warning("choosing a test machine for %s#%s failed", pr.repo, pr.number)
        return chosen
    if answer.ok and answer.structured is not None:
        picked = MachineChoice.model_validate(answer.structured).machine.strip()
        if picked in names:
            chosen = picked
    await ctx.store.set_state(state, {"key": key, "machine": chosen})
    return chosen
