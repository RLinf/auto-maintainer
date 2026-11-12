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

"""A review's session on a test machine (design 5.5, 5.10).

A review, or a follow-up, is one piece of work: the agent that reviews the PR also
tests and reproduces it, as it sees fit, on a test machine the repository uses. Before
the agent starts, the playbook asks for a session with a ``lab.review`` action (on by
default, ``domain.actions.ON_BY_DEFAULT``; a repository sets it lower like any level)
and waits until the actuator has opened it: the base and head code copied over, an
idle GPU leased when there is one. The agent then reaches the machine with ``lab``,
every command checked against the machine's rules, and the playbook closes the session
when the agent is done. Without a machine, or when the rule holds the PR back, the
review runs as before and says why nothing ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.actions import (
    ActionHints,
    ActionKind,
    ActionStatus,
    Autonomy,
    LabReproducePayload,
    Provenance,
    make_action,
)
from ..domain.events import EventKind
from ..domain.labs import first_usable, setup_lines, time_limit
from ..domain.profile import RepoProfile
from ..domain.snapshot import PrSnapshot
from ..policy.engine import PolicyFacts, level_for
from .base import PlaybookContext, PlaybookResult, Wait

REVIEW_SESSION = "review:"
"""How a review's ``lab.review`` discriminator starts; the request's id follows."""

OPEN_WAIT_SECONDS = 3600
"""How long a review waits for its session to open (the machine's lane may be busy)."""

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to write the review."""

MACHINE = """Test machine {lab} is yours while you work: run on it whatever your skill has you run.
- `lab -- 'COMMAND'` (one quoted string) runs COMMAND in a fresh shell in a copy of the PR's head on the machine, after the machine's own setup; `lab --side base -- 'COMMAND'` runs it in the code before the PR. Chain what needs one shell with `&&`, and keep environments, caches, and downloads inside the copy.
- The session holds {gpus} now. `lab --gpus N` makes it hold N, waiting up to `--wait MINUTES` for them to be idle; the commands after it see all of them. `lab --info` says what it holds and how long it has left, and `lab --timeout MINUTES -- 'COMMAND'` gives a long command more time.
- Run each `lab` command in the foreground until it exits, even when it takes many minutes; if your tool moves it to the background, wait on it there. The end of your turn ends the session.
- The working directory here is a checkout of the PR's head to read; the PR's code runs on the machine, through `lab`. To read GitHub (this PR, its reviews and CI, other PRs and issues), use gh; it reads only.
- The machine is shared: it refuses commands that would reach beyond the session (sudo, shutting it down, killing processes it did not start, stopping every Ray job, writing system folders).{setup}{about}"""

REPRODUCTION = """Where the review skill has you choose how the PR is reproduced, choose its reproduction agent, and do not start one yourself: the service runs it. Put its brief in `reproduction`, holding what the skill says a brief holds and the hardware the run needs (how many GPUs). Once your review is done, a fresh agent follows the brief on {lab}, and its record is added to the review. Leave `reproduction` empty when the skill says the PR needs no reproduction."""


def held_gpus(opened: dict[str, Any]) -> list[int]:
    """The GPUs a session held when it opened, from the outcome of its action (one
    GPU in sessions from before there could be several)."""
    gpus = opened.get("gpus")
    if gpus is None:
        gpus = [] if opened.get("gpu") is None else [opened["gpu"]]
    return [int(g) for g in gpus]


def machine_facts(
    lab: str,
    description: str,
    gpus: list[int] | None,
    setup: list[str] | None = None,
    minutes: int | None = None,
) -> str:
    """What an agent with a session on ``lab`` needs to know to use it: how ``lab``
    works, what runs before each command, how long the session lasts, and what the
    machine's maintainers wrote about it. Facts, not rules: what to run is the
    skill's to say."""
    held = (
        f"{len(gpus)} GPU{'s' if len(gpus) != 1 else ''} ({', '.join(map(str, gpus))})"
        if gpus
        else "no GPU"
    )
    about = (
        f"\n\nWhat {lab}'s maintainers say about it (where things are, mirrors, "
        f"limits):\n{description.strip()}"
        if description.strip()
        else ""
    )
    before = (
        "\n- Before each command, the session runs: " + " && ".join(setup)
        if setup
        else ""
    )
    if minutes:
        before += f"\n- The session lasts at most {minutes} minutes."
    return MACHINE.format(lab=lab, gpus=held, setup=before, about=about)


@dataclass
class ReviewMachine:
    """Where a review stands with its test machine."""

    wait: PlaybookResult | None = None
    """The session is being opened: the playbook returns this and runs again once it
    is open."""
    session_id: str | None = None
    token: str | None = None
    lab: str | None = None
    gpus: list[int] = field(default_factory=list)
    """The GPUs the session held when it opened."""
    note: str = ""
    """Why the review runs nothing, for its prompt and result; empty with a session."""
    commands: list[dict[str, object]] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """What the job's result says about testing."""
        if self.lab:
            return {
                "tested_on": self.lab,
                "commands": [
                    {k: v for k, v in c.items() if k != "tail"} for c in self.commands
                ],
            }
        return {"not_tested": self.note} if self.note else {}

    def testing(self, ctx: PlaybookContext, *, reproduce: bool) -> str:
        """What the agent is told about its session: the machine's facts, and for a
        review, that the service runs the skill's reproduction agent."""
        assert self.lab
        machine = ctx.service.machines.get(self.lab)
        binding = ctx.profile.labs.get(self.lab) if ctx.profile else None
        facts = machine_facts(
            self.lab,
            machine.description if machine else "",
            self.gpus,
            setup_lines(machine, binding) if machine else None,
            time_limit(machine, binding) // 60 if machine else None,
        )
        return facts + ("\n" + REPRODUCTION.format(lab=self.lab) if reproduce else "")


def machine_for(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, by: str | None
) -> str | None:
    """The machine a review of ``pr`` that ``by`` asked for tests on: their own first,
    else the first the repository uses that can take it."""
    return first_usable(ctx.service.machines, profile.labs, pr.repo, by)


async def review_machine(
    ctx: PlaybookContext,
    pr: PrSnapshot,
    profile: RepoProfile,
    request_id: str,
    by: str | None,
    *,
    timed_out: bool = False,
) -> ReviewMachine:
    """Ask for the review's session, or find it: wait while it opens, then hand over
    its token; or say why the review tests nothing."""
    discriminator = f"{REVIEW_SESSION}{request_id}"
    mine = next(
        (
            r
            for r in await ctx.store.list_actions(
                repo=pr.repo, number=pr.number, kinds=[ActionKind.LAB_REVIEW], limit=50
            )
            if r.action.discriminator == discriminator
        ),
        None,
    )
    if mine is None:
        lab = machine_for(ctx, pr, profile, by)
        if lab is None:
            return ReviewMachine(
                note="No test machine is set up for this repository, so nothing ran."
            )
        level = level_for(
            ActionKind.LAB_REVIEW,
            profile,
            PolicyFacts(author_is_team=ctx.trusted_input, changed_paths=pr.paths),
        )
        if level != Autonomy.AUTO:
            return ReviewMachine(
                note="The repository's settings do not let reviews of this PR run it "
                "on a test machine on their own, so nothing ran."
            )
        machine = ctx.service.machines[lab]
        action = make_action(
            ActionKind.LAB_REVIEW,
            pr.repo,
            LabReproducePayload(
                lab=lab,
                summary=f"Review #{pr.number}: test and reproduce it as the reviewing "
                "agent sees fit",
                sides=["base", "head"],
                wants_gpu=True,
                timeout_seconds=time_limit(machine, profile.labs.get(lab)),
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=discriminator,
            rationale=f"the review of #{pr.number} tests it on {lab}",
            hints=ActionHints(author_is_team=ctx.trusted_input, changed_paths=pr.paths),
            provenance=Provenance(requested_by=by),
        )
        return ReviewMachine(
            wait=PlaybookResult(
                actions=[action],
                wait=Wait(
                    kinds=[EventKind.ACTION_EXECUTED, EventKind.ACTION_FAILED],
                    seconds=OPEN_WAIT_SECONDS,
                    reason=f"waiting for a session on {lab}",
                    action_ids=[action.id],
                ),
                result={"session_action": action.id},
            )
        )
    payload = LabReproducePayload.model_validate(mine.action.payload)
    if mine.status in (ActionStatus.NEW, ActionStatus.QUEUED, ActionStatus.INTENT):
        if timed_out:
            return ReviewMachine(
                note=f"The session on {payload.lab} did not open in time, so nothing ran."
            )
        return ReviewMachine(
            wait=PlaybookResult(
                wait=Wait(
                    kinds=[EventKind.ACTION_EXECUTED, EventKind.ACTION_FAILED],
                    seconds=OPEN_WAIT_SECONDS,
                    reason=f"waiting for a session on {payload.lab}",
                    action_ids=[mine.action.id],
                ),
                result={"session_action": mine.action.id},
            )
        )
    if mine.status != ActionStatus.EXECUTED:
        why = {
            ActionStatus.SHADOWED: "this instance only records such runs",
            ActionStatus.PROPOSED: "it waits for a maintainer while the service is paused",
            ActionStatus.DENIED: "the repository's settings refused it",
        }.get(mine.status, mine.error or str(mine.status))
        return ReviewMachine(
            note=f"The session on {payload.lab} did not open ({why}), so nothing ran."
        )
    outcome = mine.external_ref
    if outcome.get("error") or outcome.get("deferred") or outcome.get("refused"):
        why = outcome.get("error") or outcome.get("reason") or outcome.get("refused")
        return ReviewMachine(
            note=f"{payload.lab} could not be used ({why}), so nothing ran."
        )
    session_id = str(outcome.get("session") or "")
    token = (
        ctx.lab_sessions.token(session_id)
        if session_id and ctx.lab_sessions is not None
        else None
    )
    if not token:
        return ReviewMachine(
            note=f"The session on {payload.lab} ended before the agent could use it, "
            "so nothing ran."
        )
    return ReviewMachine(
        session_id=session_id,
        token=token,
        lab=payload.lab,
        gpus=held_gpus(outcome),
    )


def session_task_fields(ctx: PlaybookContext, machine: ReviewMachine) -> dict[str, Any]:
    """What the review's agent task gets with a session: its token, the socket, and
    all the time the session has left plus some to write the review."""
    if not machine.token or not machine.session_id or ctx.lab_sessions is None:
        return {}
    return {
        "lab_session": machine.token,
        "env": {"AM_LAB_SOCKET": str(ctx.service.lab_socket or "")},
        "min_timeout_seconds": ctx.lab_sessions.seconds_left(machine.session_id)
        + SESSION_GRACE_SECONDS,
    }


def testing_block(machine: ReviewMachine, record: str | None) -> str:
    """What the review says about running the PR: the reproduction on record (one a
    maintainer asked for), and, without a session, why nothing ran this time."""
    parts = []
    if record:
        parts.append(
            "Reproduction on record, from the service's test machines, one a person "
            "asked for (use it where the skill asks how the PR was reproduced; say "
            "which commit it ran and whether that is the head you review): " + record
        )
    if not machine.token:
        parts.append(
            (machine.note or "Nothing ran.")
            + " Where the skill asks how the PR was tested or reproduced, say that no "
            "run was made in this review and what a run would need (hardware, data, "
            "and the documented steps)."
        )
    return "\n\n".join(parts)


async def close(ctx: PlaybookContext, machine: ReviewMachine) -> None:
    """Close the session, once, and keep the commands that ran in it."""
    if machine.session_id and ctx.lab_sessions is not None:
        closed = await ctx.lab_sessions.close(machine.session_id)
        if closed is not None:
            machine.commands = list(closed.get("commands") or [])
    machine.session_id = machine.token = None
