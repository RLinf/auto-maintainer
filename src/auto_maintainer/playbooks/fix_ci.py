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

"""A fix for a PR's failing CI, tested on a test machine (design 5.5).

A person asks for it on the PR page (``dashboard.command`` with ``command: fix_ci``),
optionally with a test machine. Their press approves the machine's session too: the
API submits it as a ``lab.reproduce`` drafted for them and approves it as they press,
and the actuator opens it. An agent reads the failed jobs, their logs, and the
service's diagnoses, and changes a checkout of the PR's head; with a session it copies
its checkout to the machine (``lab --sync``) and runs the failing steps there
(``lab -- ...``), every command checked against the machine's rules, until they pass
or it knows why not. It never commits, pushes, or posts. The playbook closes the
session and proposes the diff as a ``pr.fix`` drafted for that person, who pushes it as
themselves, as for a review's findings.

The merge schedule asks too (``schedule: true``), on a scheduled PR's turn, for the
maintainer who scheduled it, when every failed job's root cause is a static check's
finding and the repository lets the schedule fix those (``merge.schedule.fix``). Then
the agent fixes only what those checks report, asks nobody, and runs no repository
code; a fix that changes only the files their causes name, in at most
``max_lines`` lines, is drafted as a ``pr.fix`` onto the PR's branch that the
pipeline approves as the scheduler (``actuator/pipeline.py:SCHEDULED``). Any other
fix is proposed to the scheduler as usual. How it went is kept under
``merge_schedule.fix_key`` for the merge job, which it wakes.
"""

from __future__ import annotations

from typing import Any

from ..domain import forges
from ..domain.actions import (
    ActionKind,
    ActionStatus,
    FixPrPayload,
    Provenance,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
from ..domain.merge_schedule import fix_key
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, CiObservation
from ..domain.snapshot import PrSnapshot, PrState
from .base import (
    Playbook,
    PlaybookContext,
    PlaybookResult,
    Wait,
    keeps_session,
    may_succeed_later,
)
from .ci_doctor import OUTSIDE_LOGS, failing_checks, outside_logs
from .fix import DESCRIBE, FixResult
from .prompts import (
    pr_block,
    review_skill_block,
    schema_hint,
    untrusted,
    work_lessons_block,
)
from .review_session import earlier_work


def _read_log(repo: str) -> str:
    """How the agent reads a failed job, on the forge ``repo`` is on."""
    if forges.info_of(repo).kind == "github":
        return (
            f"read its log (`gh run view RUN_ID --log-failed --repo {repo}`, or `gh api` "
            "for a job's log), the workflow that runs it under `.github/workflows/`"
        )
    return (
        "read the failure the service quotes for it below, and the CI configuration "
        "that runs it in the checkout (such as `.gitlab-ci.yml`)"
    )


FIX_CI = """You are fixing the failing CI of a pull request for {who}, inside an automated maintainer service. The working directory is a checkout of the PR's head commit.
- Find why each failed job below fails: {read_log}, and the service's diagnosis when there is one.
- Fix every failed job below, whatever causes it. Where the PR's change causes it, fix it in the PR's code with the smallest change that makes the job pass and keeps the PR author's approach. Where the cause lies outside the PR (a flaky test, the CI runner, a base branch that fails without this PR), still make the smallest change in this checkout that makes the job pass, and say in your summary that the cause is outside the PR, and where.
- Do not commit, push, or post, and never ask whether to: the service shows your changes to {who}, who decides whether to push them.
{test}
- When something blocks a sound fix (the intended behavior is unclear, two fixes are equally plausible), ask {who} with your tool for asking the user.
- Write any text in {public_language}: plain and specific.
- End with a short message: the cause of each failed job, what you changed for it, what you ran to check it and what came out, which causes lie outside the PR, and what you could not fix and why."""

ON_MACHINE = """- Test your fix on test machine {lab}, never here: `lab --sync` copies this checkout, as you changed it, to the machine, and `lab -- 'COMMAND'` (one quoted string) runs a command there in a fresh shell in that copy, after the machine's own setup; `lab --timeout MINUTES -- 'COMMAND'` gives a long step more time, and `lab --info` says how long the session has left. Run the failing jobs' steps the way their workflow does (install what they install, then the failing tests or commands), and sync again after each change.
- Keep environments, downloaded checkpoints and datasets, and caches in `$AM_LAB_KEEP`, this PR's folder on the machine: later sessions of the PR get it too, while the copy of the code is fresh each session and removed with it. Look there first; an earlier session may have left what you need.
- Run each `lab` command in the foreground and wait until it exits, even when it takes many minutes: give your tool for running commands enough time, and if the tool moves it to the background anyway, wait on that task with the tool until it ends. Never end your turn while a command still runs or to continue later: the end of your turn ends the session and stops everything it started.
- When a step fails because of the machine (a missing package, a mirror, a path), fix it inside the copy and say what you changed; never use sudo or touch anything outside the session's folder and `$AM_LAB_KEEP`: the machine refuses commands that would."""

NO_MACHINE = """- Never run the repository's tests, builds, scripts, or the PR's code here: the repository's code runs only on its test machines, and none was chosen. Check what you changed by reading it and with the repository's static linters when it has them, and say which jobs and tests a run should cover."""

SCHEDULED_FIX = """- This fix is for the merge schedule, which pushes it onto the PR's branch as {who} without showing it to anyone first, so the rules above narrow: fix only what the static checks below report (linters, formatters, type checkers), with the smallest edit to the lines they name, keeping what the code does. Change no other line and no other file, never ask anything, and never run the repository's code or tests. A fix that changes another file, or more than {max_lines} lines, is not pushed."""

SESSION_GRACE_SECONDS = 600
"""How much longer than its session the agent's run may take, to finish its account."""


def failed_jobs(
    pr: PrSnapshot, profile: RepoProfile, chosen: set[str] | None = None
) -> list[str]:
    """The failed jobs at the PR's head worth fixing (``ci_doctor.failing_checks``:
    the newest runs, without the repository's gate and summary jobs), those a person
    ``chosen`` when they chose, one line each, with the run to read."""
    return [
        f"- {c.workflow + ' / ' if c.workflow else ''}{c.name}: {c.conclusion}"
        + (f" (run {c.run_id})" if c.run_id else "")
        for c in failing_checks(pr, profile)
        if not chosen or c.name in chosen
    ]


def diagnoses(observations: list[CiObservation], names: set[str]) -> str:
    """What the service's CI diagnosis found for the failed jobs at this head."""
    lines = []
    for o in observations:
        if o.check_name not in names:
            continue
        line = f"- {o.check_name} ({o.category}): {o.explanation}"
        cause = o.root_cause
        if cause is not None:
            line += f"\n  Cause ({cause.origin}): {cause.failing}: {cause.cause}"
            if cause.fix:
                line += f"\n  Suggested fix: {cause.fix}"
            if cause.evidence:
                line += "\n  Log: " + " | ".join(cause.evidence[:5])
        lines.append(line)
    return "\n".join(lines)


def static_files(observations: list[CiObservation], names: set[str]) -> set[str]:
    """The files that the static checks' findings among the failed jobs ``names``
    name (``RootCause.static``): all the schedule's own fix may change."""
    return {
        c.path
        for o in observations
        if o.check_name in names and o.root_cause is not None and o.root_cause.static
        for c in o.root_cause.culprits
    }


def changed_lines(patch: str) -> int:
    """Added and removed lines in a ``git diff``."""
    return sum(
        1
        for line in patch.splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    )


def _request(ctx: PlaybookContext, event: Event) -> Event:
    """The request that started the job: its first event (later ones only wake it)."""
    return Event.model_validate(ctx.job.events[0]) if ctx.job.events else event


class FixCiPlaybook(Playbook):
    name = "fix_ci"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event, profile) -> bool:  # type: ignore[no-untyped-def]
        return (
            event.payload.get("command") == "fix_ci"
            and bool(event.payload.get("by"))
            and event.number is not None
        )

    def key(self, event: Event) -> str:
        return f"fix_ci:{event.repo}#{event.number}:req:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        request = _request(ctx, event)
        assert request.repo and request.number
        by = str(request.payload["by"])
        ctx.requested_by = by
        ctx.agent = request.payload.get("agent") or None
        pr = await ctx.forge.get_pr(request.repo, request.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        scheduled = bool(request.payload.get("schedule"))
        plan = profile.merge.schedule.fix
        armed = 0
        if scheduled:
            arm = await ctx.store.get_arm(pr.repo, pr.number)
            tried = await ctx.store.get_state(fix_key(pr.repo, pr.number)) or {}
            if (
                arm is None
                or arm.armed_by.lower() != by.lower()
                or not same_commit(arm.sha, pr.head_sha)
                or not same_commit(tried.get("sha"), pr.head_sha)
                or tried.get("outcome") != "started"
            ):
                return PlaybookResult(
                    result={"skipped": f"{by} does not schedule it at this head"}
                )
            armed = int(arm.armed_at.timestamp())
            if not plan.enabled:
                return await self._refused(
                    ctx, pr, "the repository does not let the schedule fix CI"
                )
        chosen = set(request.payload.get("jobs") or [])
        jobs = failed_jobs(pr, profile, chosen)
        outside = await outside_logs(ctx, pr, profile, chosen)
        if not jobs:
            if scheduled:
                return await self._refused(ctx, pr, "no CI job failed at its head")
            return PlaybookResult(result={"skipped": "no CI job failed at its head"})

        session_id: str | None = None
        machine_note = ""
        lab_action = "" if scheduled else str(request.payload.get("lab_action") or "")
        if lab_action:
            record = await ctx.store.get_action(lab_action)
            if record is not None and record.status in (
                ActionStatus.NEW,
                ActionStatus.PROPOSED,
                ActionStatus.QUEUED,
                ActionStatus.INTENT,
            ):
                return PlaybookResult(
                    wait=Wait(
                        kinds=[EventKind.ACTION_EXECUTED, EventKind.ACTION_FAILED],
                        seconds=6 * 3600,
                        reason="waiting for the test machine's session to open",
                        action_ids=[lab_action],
                    ),
                    result={"action": lab_action},
                )
            session_id, machine_note = self._session(record)

        ctx.trusted_input = await ctx.author_is_team(pr)
        names = {
            c.name
            for c in failing_checks(pr, profile)
            if not chosen or c.name in chosen
        }
        observations = await ctx.store.list_ci_observations(pr.repo, sha=pr.head_sha)
        found = diagnoses(observations, names)
        info = await ctx.forge.get_repo(pr.repo)
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, info.default_branch
        )
        answer = described = None
        commands: list[dict[str, Any]] = []
        token = (
            ctx.lab_sessions.token(session_id)
            if session_id and ctx.lab_sessions is not None
            else None
        )
        try:
            if token and ctx.lab_sessions is not None and session_id:
                ctx.lab_sessions.allow_sync(session_id, workdir)
            lab = str(request.payload.get("lab") or "")
            note = str(request.payload.get("note") or "").strip()
            diff = await ctx.workspace.diff(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            machine = ctx.service.machines.get(lab) if token else None
            prompt = "\n\n".join(
                part
                for part in [
                    FIX_CI.format(
                        who=f"{by}, the maintainer who scheduled it to merge"
                        if scheduled
                        else f"{by}, a maintainer",
                        read_log=_read_log(pr.repo),
                        test=ON_MACHINE.format(lab=lab) if token else NO_MACHINE,
                        public_language=profile.languages.public,
                    ),
                    SCHEDULED_FIX.format(who=by, max_lines=plan.max_lines)
                    if scheduled
                    else "",
                    machine_note,
                    f"The environment and rules of {lab}, from its maintainers; follow "
                    f"them:\n{machine.description.strip()}"
                    if machine and machine.description.strip()
                    else "",
                    review_skill_block(profile, ctx.service.skills_dir, workdir),
                    work_lessons_block(
                        await ctx.knowledge.agent_lessons(
                            pr.repo, [lab] if token and lab else []
                        )
                    ),
                    await earlier_work(ctx, pr) if token else "",
                    "Failed CI jobs at the PR's head "
                    f"{short_sha(pr.head_sha)}:\n" + "\n".join(jobs),
                    "The service's diagnosis of them:\n"
                    + untrusted("ci.diagnosis", found, 12_000)
                    if found
                    else "",
                    OUTSIDE_LOGS + untrusted("ci.outside_logs", outside, 40_000)
                    if outside
                    else "",
                    f"What {by} added: " + untrusted("maintainer.note", note, 2000)
                    if note
                    else "",
                    pr_block(pr, diff),
                ]
                if part
            )
            seconds = 2400
            if token and ctx.lab_sessions is not None and session_id:
                seconds = (
                    max(seconds, ctx.lab_sessions.seconds_left(session_id))
                    + SESSION_GRACE_SECONDS
                )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt=prompt,
                    workdir=workdir,
                    mode="workspace_write",
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    pr=pr.number,
                    min_timeout_seconds=seconds,
                    lab_session=token,
                    env={"AM_LAB_SOCKET": str(ctx.service.lab_socket or "")}
                    if token
                    else {},
                )
            )
            account = answer.text.strip() if answer.ok else ""
            changes = await ctx.workspace.changes(workdir)
            if answer.ok and changes.files:
                described = await ctx.ask(
                    AgentTask(
                        kind=TaskKind.FIX,
                        prompt="\n\n".join(
                            [
                                DESCRIBE.format(
                                    public_language=profile.languages.public
                                ),
                                "The agent was asked to fix these failed CI jobs; "
                                "use each job's name as its id:\n" + "\n".join(jobs),
                                *(
                                    [untrusted("agent.account", account[-4000:], 4000)]
                                    if account
                                    else []
                                ),
                                untrusted("fix.diff", changes.patch, 60_000),
                                schema_hint(FixResult.model_json_schema()),
                            ]
                        ),
                        workdir=workdir,
                        mode="read_only",
                        repo=pr.repo,
                        untrusted_input=True,
                    ),
                    FixResult,
                )
        finally:
            await ctx.workspace.release(workdir)
            keep = keeps_session(ctx, answer)
            if session_id and ctx.lab_sessions is not None and not keep:
                closed = await ctx.lab_sessions.close(session_id)
                commands = list((closed or {}).get("commands") or [])
        if answer is None or not answer.ok:
            error = answer.error if answer else "no answer"
            if answer is not None and may_succeed_later(answer):
                raise RuntimeError(f"the CI fix agent failed: {error}")
            if scheduled:
                return await self._refused(ctx, pr, f"the agent failed: {error}")
            return PlaybookResult(failed=f"the CI fix agent failed: {error}")
        ran = [
            f"- `{c.get('command')}`: "
            + (
                "refused"
                if c.get("refused")
                else f"exit {c.get('exit_code')}"
                + (", timed out" if c.get("timed_out") else "")
            )
            for c in commands
        ]
        if not changes.files:
            if scheduled:
                return await self._refused(
                    ctx,
                    pr,
                    "the agent changed nothing"
                    + (f": {account[-400:]}" if account else ""),
                )
            return PlaybookResult(
                result={
                    "skipped": "the agent changed nothing",
                    "summary": account[-2000:],
                    "commands": ran,
                    **({"tested_on": lab, "commit": pr.head_sha} if ran else {}),
                }
            )
        if described is not None and described.ok and described.structured is not None:
            fix = FixResult.model_validate(described.structured)
        else:
            fix = FixResult(
                title=f"Fix failing CI on #{pr.number}",
                summary=account[-2000:] or "Fixes for the failing CI jobs.",
            )
        lines = [fix.summary, ""]
        lines += [f"- {f.id}: {f.change}" for f in fix.fixed]
        if fix.skipped:
            lines += ["", "Not changed:"] + [
                f"- {f.id}: {f.reason}" for f in fix.skipped
            ]
        lines += ["", f"For #{pr.number}, from its CI at {short_sha(pr.head_sha)}."]
        outcome = {
            "files": changes.files,
            "fixed": [f.id for f in fix.fixed],
            "commands": ran,
            "summary": account[-2000:],
            **({"tested_on": lab, "commit": pr.head_sha} if ran else {}),
        }
        if scheduled:
            beyond = sorted(set(changes.files) - static_files(observations, names))
            size = changed_lines(changes.patch)
            if not beyond and size <= plan.max_lines:
                lines += [
                    "",
                    f"Pushed by the merge schedule for {by}, who scheduled the PR.",
                ]
                action = make_action(
                    ActionKind.PR_FIX,
                    pr.repo,
                    FixPrPayload(
                        title=fix.title.strip()[:120] or f"Fix lint on #{pr.number}",
                        body="\n".join(lines).strip(),
                        patch=changes.patch,
                        base_sha=pr.head_sha,
                        files=changes.files,
                        findings=[f.id for f in fix.fixed] or sorted(names),
                        runtime=answer.runtime,
                        requested_by=by,
                        target="branch",
                        schedule_fix=True,
                    ),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    # One per schedule of the PR and head.
                    discriminator=f"schedule:{armed}:fix:{pr.head_sha[:12]}",
                    rationale=f"fixes what {', '.join(sorted(names))} reports, in "
                    f"{size} lines; scheduled by {by}",
                    draft_for=by,
                    provenance=Provenance(requested_by=by, scheduled_by=by),
                )
                await self._noted(ctx, pr, "proposed", action_id=action.id)
                return PlaybookResult(
                    actions=[action], events=[self._wake(pr)], result=outcome
                )
            why = (
                f"its fix also changes {', '.join(beyond[:5])}, which the static "
                "checks do not name"
                if beyond
                else f"its fix changes {size} lines, more than the "
                f"{plan.max_lines} the schedule pushes by itself"
            )
            await self._noted(
                ctx, pr, "refused", f"{why}; it waits for {by} under Approvals"
            )
        action = make_action(
            ActionKind.PR_FIX,
            pr.repo,
            FixPrPayload(
                title=fix.title.strip()[:120] or f"Fix failing CI on #{pr.number}",
                body="\n".join(lines).strip(),
                patch=changes.patch,
                base_sha=pr.head_sha,
                files=changes.files,
                findings=[f.id for f in fix.fixed] or sorted(names),
                runtime=answer.runtime,
                requested_by=by,
            ),
            number=pr.number,
            head_sha=pr.head_sha,
            discriminator=f"req:{request.id}",
            rationale=f"fixes for failing CI, asked by {by}"
            + (f"; tested with {len(ran)} commands on a test machine" if ran else ""),
            draft_for=by,
        )
        return PlaybookResult(
            actions=[action],
            events=[self._wake(pr)] if scheduled else [],
            result=outcome,
        )

    @staticmethod
    def _wake(pr: PrSnapshot) -> Event:
        """Wakes the PR's merge job to read how the schedule's fix went."""
        return Event(
            kind=EventKind.MERGE_QUEUE,
            repo=pr.repo,
            number=pr.number,
            payload={"after": "fix"},
        )

    async def _noted(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        outcome: str,
        why: str = "",
        action_id: str = "",
    ) -> None:
        await ctx.store.set_state(
            fix_key(pr.repo, pr.number),
            {
                "sha": pr.head_sha,
                "outcome": outcome,
                "why": why,
                "action_id": action_id,
                "at": ctx.clock.now().isoformat(),
                "job": ctx.job.id,
            },
        )

    async def _refused(
        self, ctx: PlaybookContext, pr: PrSnapshot, why: str
    ) -> PlaybookResult:
        """The schedule's fix is not made: note why and wake the merge job."""
        await self._noted(ctx, pr, "refused", why)
        return PlaybookResult(events=[self._wake(pr)], result={"refused": why})

    @staticmethod
    def _session(record: ActionRecord | None) -> tuple[str | None, str]:
        """The open session from the approved ``lab.reproduce``, or None with why
        the fix goes on without a machine."""
        if record is None:
            return None, "The test machine's session was not found; work without it."
        if record.status != ActionStatus.EXECUTED:
            return None, (
                f"The test machine's session did not open ({record.status}"
                + (f": {record.error}" if record.error else "")
                + "); work without it."
            )
        outcome = record.external_ref
        if outcome.get("error") or outcome.get("deferred") or outcome.get("refused"):
            why = (
                outcome.get("error") or outcome.get("reason") or outcome.get("refused")
            )
            return None, f"The test machine could not be used ({why}); work without it."
        session = str(outcome.get("session") or "")
        return (
            session or None
        ), "" if session else "No session opened; work without it."
