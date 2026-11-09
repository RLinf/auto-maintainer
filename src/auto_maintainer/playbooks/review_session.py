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
from ..domain.labs import time_limit, unusable
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

ON_MACHINE = """- Test and reproduce the PR on test machine {lab} as you see fit while you review: its tests, the commands and examples its documentation gives (follow them the way a user would, and note every step they leave out), experiments that check what it claims, and the code before the PR where a comparison settles something.
- `lab -- 'COMMAND'` (one quoted string) runs a command in a fresh shell in a copy of the PR's head on the machine, after the machine's own setup; `lab --side base -- 'COMMAND'` runs it in the code before the PR. Chain what needs one shell with `&&`, keep environments, caches, and downloads inside the copy, and activate an environment in each command that needs it. `lab --timeout MINUTES -- 'COMMAND'` gives a long step more time; `lab --info` says how long the session has left and whether it has a GPU (without one, say which steps need a GPU and could not run).
- Run each `lab` command in the foreground and wait until it exits, even when it takes many minutes: give your tool for running commands enough time, and if the tool moves it to the background anyway, wait on that task with the tool until it ends. Never end your turn while a command still runs: the end of your turn ends the session.
- The working directory here is a checkout of the PR's head to read; the PR's code runs only on the machine, through `lab`. To read GitHub (this PR's reviews and comments, other PRs, issues, CI runs), use gh; it reads only.
- When a step fails because of the machine or the setup rather than the PR (a missing package, a path, a mirror or download), find the cause, fix it inside the copy, and say what you changed; {stuck} A failure the PR causes is a finding.
- Never use sudo, change anything outside the session's folders, or stop other people's work on the machine; the machine refuses commands that would.
- Spend the time on what matters for this review: what the PR changes and claims. Report what you ran, on what kind of machine, and what came out where the review skill asks how the PR was tested or reproduced (or in the summary), with the first failure and the steps the docs left out; a claim the runs disprove is a finding."""

STUCK_ASK = "when you cannot, ask {by} with your tool for asking the user."
STUCK_NOTE = "when you cannot, say what the machine needs and go on with the review."


@dataclass
class ReviewMachine:
    """Where a review stands with its test machine."""

    wait: PlaybookResult | None = None
    """The session is being opened: the playbook returns this and runs again once it
    is open."""
    session_id: str | None = None
    token: str | None = None
    lab: str | None = None
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

    def testing(self, by: str | None) -> str:
        """The prompt's rules on running code, with this session."""
        stuck = STUCK_ASK.format(by=by) if by else STUCK_NOTE
        return ON_MACHINE.format(lab=self.lab, stuck=stuck)


def machine_for(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile
) -> str | None:
    """The first test machine the repository uses that can take ``pr``."""
    return next(
        (
            name
            for name, binding in profile.labs.items()
            if binding.use
            and unusable(pr.repo, name, ctx.service.machines.get(name), binding) is None
        ),
        None,
    )


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
        lab = machine_for(ctx, pr, profile)
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
    return ReviewMachine(session_id=session_id, token=token, lab=payload.lab)


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
