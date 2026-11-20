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
tests it, as it sees fit, on a test machine the repository uses, whether or not
someone asked for the review. Before the agent starts, the playbook asks for a session
with a ``lab.review`` action (on by default, ``domain.actions.ON_BY_DEFAULT``; a
repository sets it lower like any level) and waits until the actuator has opened it:
the base and head code copied over, no GPU until a check takes one. The agent then
reaches the machine with ``lab``, every command checked against the machine's rules:
it runs the skill's checks and its own small experiments to confirm or rule out what
it suspects. Without a machine, or when a rule holds the PR back, the review runs
without one and says why nothing ran.

Whether the PR needs a reproduction (its documented workflow run as a user would) is
the agent's to decide, by its skill. The skill's reproduction agent is a fresh agent,
apart from the one that read the code: the reviewer starts it with ``lab
--reproduce`` and a brief (``Service.start_review_reproduction``), and it runs on the
same machine while the review goes on (``reproduce.py``). When the reviewer's turn
ends, the review waits for that run (``reproduction_outcome``), then resumes the same
agent with the run's record (``FINISH``), so the review reports the reproduction in
one piece. The session stays open meanwhile, for checks the run's result calls for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from ..domain.labs import setup_lines, time_limit
from ..domain.profile import RepoProfile
from ..domain.records import JobStatus
from ..domain.snapshot import PrSnapshot
from ..policy.engine import PolicyFacts, level_for
from .base import PlaybookContext, PlaybookResult, Wait
from .machine_choice import choose_machine
from .prompts import untrusted

REVIEW_SESSION = "review:"
"""How a review's ``lab.review`` discriminator starts; the request's id follows."""

OPEN_WAIT_SECONDS = 3600
"""How long a review waits for its session to open (the machine's lane may be busy)."""

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to write the review."""

REVIEW_REPRODUCTION = "review_reproduction:{job}"
"""The reproduction a review's agent started: the request's event id, the machine,
and when; once the run ended, its ``record``; ``used`` once the review took it."""

REPRODUCTION_WAIT_SECONDS = 1800
"""How long a review waits at a time for its reproduction (a finished job wakes it)."""

REPRODUCTION_GIVE_UP_SECONDS = 12 * 3600
"""How long after it started a review stops waiting for its reproduction."""

MACHINE = """Test machine {lab} is yours while you work: run on it whatever your skill has you run.
- `lab -- 'COMMAND'` (one quoted string) runs COMMAND in a fresh shell in a real git worktree of the PR's head on the machine, after the machine's own setup; `lab --side base -- 'COMMAND'` runs it in the code before the PR. Chain what needs one shell with `&&`.
- Keep environments, downloaded checkpoints and datasets, and caches in `$AM_LAB_KEEP`, this PR's folder on the machine: later sessions of the PR get it too, while the copies of the code are fresh each session and removed with it. Look there first; an earlier session may have left what you need.
- Idle GPUs visible now via `CUDA_VISIBLE_DEVICES`: {gpus}. `lab --gpus N` refreshes idle GPUs (waiting up to `--wait MINUTES` until N are idle); `lab --info` shows status, and `lab --timeout MINUTES -- 'COMMAND'` sets a per-command limit (active downloads and runs extend the session automatically).
- Run each `lab` command in the foreground until it exits, even when it takes many minutes; if your tool moves it to the background, wait on it there. The end of your turn ends the session and stops everything it started, servers in the background too.
- The working directory here is a checkout of the PR's head to read; the PR's code runs on the machine, through `lab`. To read GitHub (this PR, its reviews and CI, other PRs and issues), use gh; it reads only.
- The machine is shared: it refuses commands that would reach beyond the session (sudo, shutting it down, killing processes it did not start, stopping every Ray job, writing system folders).{setup}{about}"""

EXPERIMENTS = """Use the machine to test what you read, not only to run the skill's checks: when you suspect a bug, a regression, or a claim that may not hold, write a small script or test that shows it, run it on the head (and with `--side base` to compare), and give what it showed as the finding's evidence. Report only what you ran and saw."""

REPRODUCTION = """Whether this PR needs a reproduction is yours to decide, as the skill says. Follow your skill's reproduction workflow directly using `lab --`, or start the skill's reproduction agent with `lab --reproduce`, giving it on standard input the brief the skill describes (`lab --reproduce <<'EOF'` ... `EOF`)."""

FINISH = """The reproduction you started has ended; its record is below. Finish your review with it, as the skill says: fill in where the skill asks how the PR was reproduced from the record (the settings it used, the functional and the metric result, the first failure and every intervention, and whether the documentation could be followed), and change a finding or the verdict only where the run settles it.{session} Then give your whole review again, as before.

{record}"""
"""The turn that resumes a review's agent once its reproduction ended."""

EARLIER = """What earlier agents did on test machines for this PR, newest first: when, at which commit, what they concluded, and the commands they ran with their exit codes. Start from it: what they set up for this PR may still be in `$AM_LAB_KEEP`; what they established at the same commit need not be shown again; what failed for a reason that still holds need not be tried the same way. It is a record of what ran, not a verdict: report only what you saw yourself."""

EARLIER_PLAYBOOKS = ("reproduce", "review", "followup", "fix_ci")
EARLIER_RUNS = 2
"""How many earlier runs on a machine a fallback session is told about."""
EARLIER_COMMANDS = 8
"""How many commands of each same-commit run: its first ones and its last ones."""
EARLIER_OLD_COMMANDS = 4
"""How many commands of an older-commit run."""


def _command_line(c: object) -> str:
    if isinstance(c, str):
        return c.strip()
    if not isinstance(c, dict):
        return ""
    command = " ".join(str(c.get("command") or "").split())[:240]
    if c.get("refused"):
        outcome = "refused"
    else:
        outcome = f"exit {c.get('exit_code')}"
        if c.get("timed_out"):
            outcome += ", timed out"
        if c.get("duration_seconds") is not None:
            outcome += f", {round(float(c['duration_seconds']))} s"
    side = f" ({c['side']})" if c.get("side") and c.get("side") != "head" else ""
    return f"- [{outcome}]{side} {command}"


async def earlier_work(ctx: PlaybookContext, pr: PrSnapshot) -> str:
    """What earlier reviews, reproductions, and CI fixes of ``pr`` ran on test
    machines (``EARLIER``), for a session that starts: so it builds on them instead
    of finding the machine and the PR's setup out again."""
    try:
        jobs = await ctx.store.list_jobs(
            repo=pr.repo, number=pr.number, playbooks=list(EARLIER_PLAYBOOKS), limit=40
        )
    except Exception:  # the record helps; a session goes on without it
        return ""
    runs: list[str] = []
    for job in jobs:
        result = job.result or {}
        commands = [
            line for line in map(_command_line, result.get("commands") or []) if line
        ]
        if job.id == ctx.job.id or not (commands or result.get("conclusion")):
            continue
        commit = str(result.get("commit") or "")
        at = (
            f"at {commit[:10]}"
            + (" (the PR's head now)" if commit == pr.head_sha else "")
            if commit
            else "at an earlier commit"
        )
        head = (
            f"{job.updated_at:%Y-%m-%d %H:%M} UTC, {job.playbook}"
            f" on {result.get('tested_on') or result.get('lab') or 'a test machine'}, {at}"
        )
        lines = [head]
        if result.get("conclusion"):
            lines.append(f"Conclusion: {result['conclusion']}")
        if result.get("notes"):
            lines.append(f"Notes: {str(result['notes'])[:600]}")
        cmd_limit = EARLIER_COMMANDS if commit == pr.head_sha else EARLIER_OLD_COMMANDS
        if len(commands) > cmd_limit:
            first = max(1, cmd_limit // 2)
            skipped = len(commands) - cmd_limit
            commands = [
                *commands[:first],
                f"- … {skipped} more …",
                *commands[-(cmd_limit - first) :],
            ]
        if commands:
            lines.append("Commands:\n" + "\n".join(commands))
        runs.append("\n".join(lines))
        if len(runs) >= EARLIER_RUNS:
            break
    if not runs:
        return ""
    return EARLIER + "\n" + untrusted("earlier.work", "\n\n".join(runs), 6_000)


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
    suited_for: str = "",
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
        f"\n\nWhat {lab} is for, as its maintainers put it:\n{suited_for.strip()}"
        if suited_for.strip()
        else ""
    ) + (
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
    """The idle GPUs visible to the session when it opened."""
    note: str = ""
    """Why the review runs nothing, for its prompt and result; empty with a session."""
    commands: list[dict[str, object]] = field(default_factory=list)

    def summary(self, commit: str | None = None) -> dict[str, object]:
        """What the job's result says about testing (and at which ``commit``, which
        later sessions of the PR read in ``earlier_work``)."""
        if self.lab:
            return {
                "tested_on": self.lab,
                **({"commit": commit} if commit else {}),
                "commands": [
                    {k: v for k, v in c.items() if k != "tail"} for c in self.commands
                ],
            }
        return {"not_tested": self.note} if self.note else {}

    def testing(self, ctx: PlaybookContext, *, reproduce: bool) -> str:
        """What the agent is told about its session: the machine's facts, that its
        own experiments belong there, and, with ``reproduce``, how it starts the
        skill's reproduction agent when it judges the PR needs one."""
        assert self.lab
        machine = ctx.service.machines.get(self.lab)
        binding = ctx.profile.labs.get(self.lab) if ctx.profile else None
        facts = machine_facts(
            self.lab,
            machine.description if machine else "",
            self.gpus,
            setup_lines(machine, binding) if machine else None,
            time_limit(machine, binding) // 60 if machine else None,
            machine.suited_for if machine else "",
        )
        return (
            facts
            + "\n"
            + EXPERIMENTS
            + ("\n" + REPRODUCTION.format(lab=self.lab) if reproduce else "")
        )


async def machine_for(
    ctx: PlaybookContext, pr: PrSnapshot, profile: RepoProfile, by: str | None
) -> str | None:
    """The machine a review of ``pr`` that ``by`` asked for tests on: of their own
    and the repository's, the one suited to the PR (``machine_choice``)."""
    return await choose_machine(ctx, pr, profile, by)


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
        lab = await machine_for(ctx, pr, profile, by)
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
                # No GPU until a check needs one: the reproduction agent beside the
                # review may need every GPU the machine has.
                wants_gpu=False,
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
    """What the review's agent task gets with a session: its lab session token/socket
    and headroom for large checkpoint downloads plus reproduction and review."""
    if not machine.token or not machine.session_id or ctx.lab_sessions is None:
        return {}
    left = ctx.lab_sessions.seconds_left(machine.session_id)
    return {
        "lab_session": machine.token,
        "env": {"AM_LAB_SOCKET": str(ctx.service.lab_socket or "")},
        "min_timeout_seconds": max(left * 2, 7200) + SESSION_GRACE_SECONDS,
    }


def testing_block(record: str | None) -> str:
    """The reproduction on record (one a person asked for), for the review."""
    if not record:
        return ""
    return (
        "A reproduction a person asked for is on record (use it where the skill asks "
        "how the PR was reproduced, and say which commit it ran): " + record
    )


async def close(ctx: PlaybookContext, machine: ReviewMachine) -> None:
    """Close the session, once, and keep the commands that ran in it."""
    if machine.session_id and ctx.lab_sessions is not None:
        closed = await ctx.lab_sessions.close(machine.session_id)
        if closed is not None:
            machine.commands = list(closed.get("commands") or [])
    machine.session_id = machine.token = None


# ---- the reproduction a review's agent starts ---------------------------------------


async def give_record(
    store: Any, review_job: str, request_id: str, record: dict[str, Any]
) -> bool:
    """Hand the record of the reproduction ``request_id`` to the review job that
    started it, which waits for it; ``False`` when no review waits for this run."""
    key = REVIEW_REPRODUCTION.format(job=review_job)
    kept = await store.get_state(key)
    if not isinstance(kept, dict) or kept.get("event") != request_id:
        return False
    await store.set_state(key, {**kept, "record": record})
    return True


def _record_text(record: dict[str, Any]) -> str:
    if record.get("not_run"):
        return f"The reproduction did not run: {record['not_run']}."
    if record.get("conclusion") == "not_needed":
        return (
            "The reproduction agent judged that the PR needs no reproduction and ran "
            "nothing: " + str(record.get("comment") or "")
        )
    return str(record.get("record_text") or record.get("comment") or "")


async def reproduction_outcome(
    ctx: PlaybookContext, pr: PrSnapshot
) -> PlaybookResult | str | None:
    """Where the reproduction this job's agent started stands: ``None`` when it
    started none (or the review already took its record); a wait while it runs; once
    it ended, its record for the agent's last turn (``FINISH``)."""
    key = REVIEW_REPRODUCTION.format(job=ctx.job.id)
    kept = await ctx.store.get_state(key)
    if not isinstance(kept, dict) or not kept.get("event") or kept.get("used"):
        return None
    record = kept.get("record")
    if not isinstance(record, dict):
        job = await ctx.store.find_job(
            "reproduce", f"reproduce:{pr.repo}#{pr.number}:req:{kept['event']}"
        )
        started = datetime.fromisoformat(str(kept.get("at")))
        if job is not None and job.status in (JobStatus.DONE, JobStatus.FAILED):
            record = {
                "not_run": f"it ended without a record ({job.status.value})"
                + (f": {job.error}" if job.error else "")
            }
        elif ctx.clock.now() - started > timedelta(
            seconds=REPRODUCTION_GIVE_UP_SECONDS
        ):
            record = {
                "not_run": "it had not finished "
                f"{REPRODUCTION_GIVE_UP_SECONDS // 3600} hours after it started"
            }
        else:
            return PlaybookResult(
                wait=Wait(
                    kinds=[EventKind.JOB_FINISHED],
                    seconds=REPRODUCTION_WAIT_SECONDS,
                    reason=f"waiting for the reproduction its agent started on "
                    f"{kept.get('lab') or 'the test machine'}",
                ),
                result={"reproduction": kept["event"]},
            )
    return _record_text(record)


async def reproduction_taken(ctx: PlaybookContext) -> None:
    """Mark the reproduction's record as in the review, so a later try does not ask
    the agent again."""
    key = REVIEW_REPRODUCTION.format(job=ctx.job.id)
    kept = await ctx.store.get_state(key)
    if isinstance(kept, dict):
        await ctx.store.set_state(key, {**kept, "used": True})


async def reproduction_running(ctx: PlaybookContext) -> bool:
    """Whether this job's agent started a reproduction whose record the review has
    not taken yet."""
    kept = await ctx.store.get_state(REVIEW_REPRODUCTION.format(job=ctx.job.id))
    return isinstance(kept, dict) and bool(kept.get("event")) and not kept.get("used")


def finish_prompt(record: str, machine: ReviewMachine) -> str:
    """The agent's last turn: the run's record, and whether its session is still
    open for a check the run calls for."""
    session = (
        f" Your session on {machine.lab} is still open, for a check the run's result "
        "calls for."
        if machine.token
        else ""
    )
    return FINISH.format(
        session=session, record=untrusted("reproduction", record, 40_000)
    )
