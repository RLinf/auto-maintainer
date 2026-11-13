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

"""Merging a PR's base branch into it when they conflict, for a maintainer (design 5.5).

A maintainer asks from the PR page or, for a self-improvement PR on ``code_repo``, from
the Service page (``dashboard.command`` with ``command: merge_base``). The playbook
starts the merge in a checkout of the PR's head; an agent resolves each conflict,
keeping what both sides meant, and may ask the maintainer; it never commits, pushes, or
runs the code. On a watched repository, the playbook proposes a ``pr.fix`` action with
``merge_ref`` drafted for that maintainer, who pushes the merge commit as themselves; on
``code_repo`` (``service_pr: true``), it proposes a ``code.update`` action for the owner
to approve and push onto the ``am/self/*`` branch. A merge without conflicts does not
come here: the dashboard updates the branch through the forge at once.

The merge schedule asks too (``command: resolve_conflicts``), on a scheduled PR's
turn, for the maintainer who scheduled it, when the repository lets the schedule
resolve conflicts (``merge.schedule.resolve``). Then only minor conflicts are
resolved: every conflicted file must be of an enabled kind (``domain/conflicts``),
nobody is asked anything, and, with ``union_only``, the resolution must combine both
sides; anything else is refused, with why, for a person. The ``pr.fix`` is drafted
for the scheduler, and the pipeline approves it as them, with their own sign-in only
(``actuator/pipeline.py:SCHEDULED``). How it went is kept under
``merge_schedule.resolve_key`` for the merge job, which it wakes.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..domain.actions import (
    Action,
    ActionKind,
    CodeUpdatePayload,
    FixPrPayload,
    Provenance,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.conflicts import kind_of, union_problem, unresolved
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit, short_sha
from ..domain.merge_schedule import resolve_key
from ..domain.snapshot import PrSnapshot, PrState
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import no_code_rule, pr_block, untrusted

RESOLVE_COMMAND = "resolve_conflicts"
"""The merge schedule's request to resolve a scheduled PR's minor conflicts."""

RESOLVE = """You are resolving merge conflicts for {reviewer}, a maintainer, inside an automated maintainer service. The working directory is a checkout of PR #{number}'s head with `{base}` merged into it; the files listed below still have conflict markers.
- Resolve every conflict in those files, keeping what both sides meant: the PR's change and what landed on `{base}` since it branched. When they cannot both stand, keep `{base}`'s behavior and adapt the PR's change to it.
- Remove every conflict marker. Change nothing outside the conflicted files, and do not commit, push, or post.
{no_code}
- When a conflict cannot be resolved without a decision only {reviewer} can make, ask them with your tool for asking the user.
- End with a short message in {public_language}: how you resolved each file, and what a test run on a test machine should check."""

RESOLVE_MINOR = """You are resolving conflicts between pull request #{number} and `{base}` for the merge schedule of {repo}, inside an automated maintainer service, which resolves by itself only conflicts that change no code's meaning. The working directory is a checkout of the PR's head with `{base}` merged into it; the files listed below still have conflict markers, each with the kind of conflict the repository lets the schedule resolve.
- Resolve a file's conflicts only when they are of the kind given for it: combine both sides, keeping everything each side added, in the file's own order and format. Write nothing that neither side has: no new wording, entries, or code.
- When a conflict is not of its kind, or resolving it takes a decision (both sides changed the same thing in different ways), leave that file's conflict markers in place and say why: a person resolves it.
- Change nothing outside the conflicted files. Do not commit, push, post, or run anything, and do not ask anyone: nobody waits on this.
- End with a short message in {public_language}: how you resolved each file, or why you left it."""


def _git_path(raw: str, prefix: str = "") -> str | None:
    text = raw.rstrip("\r\n\t")
    if text == "/dev/null":
        return None
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = re.sub(r'\\(["\\])', r"\1", text[1:-1])
    return text[len(prefix) :] if prefix and text.startswith(prefix) else text


def _block_paths(lines: list[str]) -> set[str]:
    paths: set[str] = set()
    head = lines[0].removeprefix("diff --git ").rstrip("\r\n")
    if head.startswith("a/"):
        n = len(head) - 5
        if (
            n >= 0
            and n % 2 == 0
            and head[2 + n // 2 : 5 + n // 2] == " b/"
            and head[2 : 2 + n // 2] == head[5 + n // 2 :]
        ):
            paths.add(head[5 + n // 2 :])
    elif head.startswith('"') and head.endswith('"'):
        n = len(head) - 1
        if n >= 0 and n % 2 == 0 and head[n // 2] == " ":
            left = _git_path(head[: n // 2], "a/")
            right = _git_path(head[n // 2 + 1 :], "b/")
            if left and left == right:
                paths.add(right)
    for line in lines[1:]:
        if line.startswith(("@@ ", "GIT binary patch", "Binary files ")):
            break
        for marker, prefix in (
            ("--- ", "a/"),
            ("+++ ", "b/"),
            ("rename from ", ""),
            ("rename to ", ""),
            ("copy from ", ""),
            ("copy to ", ""),
        ):
            if line.startswith(marker):
                path = _git_path(line[len(marker) :], prefix)
                if path:
                    paths.add(path)
        if line.startswith("+++ "):
            break
    if not paths and " b/" in head:
        path = _git_path(head.split(" b/", 1)[1])
        if path:
            paths.add(path)
    return paths


def only_files(patch: str, files: set[str]) -> str:
    """The part of ``patch`` that changes ``files``: what a maintainer reads of a
    merge is its resolution, not everything the base branch brings."""
    if not files:
        return ""
    blocks: list[list[str]] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return "".join("".join(b) for b in blocks if _block_paths(b) & files)


class MergeBasePlaybook(Playbook):
    name = "merge_base"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"
    needs_profile = False

    def accepts(self, event: Event, profile: object) -> bool:
        command = event.payload.get("command")
        if command == RESOLVE_COMMAND:
            return profile is not None and bool(event.payload.get("by"))
        if command != "merge_base":
            return False
        return profile is not None or bool(event.payload.get("service_pr"))

    def key(self, event: Event) -> str:
        return f"merge_base:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        if event.payload.get("command") == RESOLVE_COMMAND:
            return await self._for_schedule(ctx, event)
        service_pr = bool(event.payload.get("service_pr"))
        profile = None if service_pr else ctx.require_profile()
        assert event.repo and event.number
        if service_pr and event.repo != ctx.service.code_repo:
            return PlaybookResult(failed="not the service's code repository")
        by = str(event.payload["by"])
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        if service_pr and (
            not pr.head_ref.startswith("am/self/")
            or pr.head_repo not in (None, "", pr.repo)
        ):
            return PlaybookResult(
                failed=f"{pr.repo}#{pr.number} is not a self-improvement PR"
            )
        if service_pr:
            ctx.trusted_input = True
            public_language = "en"
            default_branch = ctx.service.code_branch
        else:
            assert profile is not None
            ctx.trusted_input = await ctx.author_is_team(pr)
            public_language = profile.languages.public
            default_branch = (await ctx.forge.get_repo(pr.repo)).default_branch
        base = pr.base_ref or default_branch
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, default_branch
        )
        try:
            attempt = await ctx.workspace.merge(workdir, base)
            if not attempt.conflicts:
                return PlaybookResult(
                    result={
                        "skipped": f"{base} merges into the PR without conflicts; "
                        "update the branch instead"
                    }
                )
            if attempt.steering:
                return PlaybookResult(
                    failed=f"{base} conflicts with the PR in files that steer coding "
                    "agents, which no agent resolves from a PR's side: "
                    + ", ".join(attempt.steering)
                    + ". Resolve the merge by hand, or have the author do it.",
                    result={"steering": attempt.steering},
                )
            listing = "\n".join(f"- {name}" for name in attempt.conflicts)
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt="\n\n".join(
                        [
                            RESOLVE.format(
                                reviewer=by,
                                number=pr.number,
                                base=base,
                                no_code=no_code_rule(pr.repo),
                                public_language=public_language,
                            ),
                            "Files with conflicts:\n"
                            + untrusted("merge.conflicts", listing, 8_000),
                            pr_block(pr),
                        ]
                    ),
                    workdir=workdir,
                    mode="workspace_write",
                    skill_dirs=ctx.skill_dirs(),
                    repo=pr.repo,
                    timeout_seconds=2400,
                )
            )
            if not answer.ok:
                if may_succeed_later(answer):
                    raise RuntimeError(f"the merge agent failed: {answer.error}")
                return PlaybookResult(failed=f"the merge agent failed: {answer.error}")
            resolved: dict[str, str] = {}
            left: list[str] = []
            for name in attempt.conflicts:
                path = Path(workdir) / name
                text = path.read_text(encoding="utf-8", errors="replace")
                if unresolved(text):
                    left.append(name)
                resolved[name] = text
            shown = only_files(
                (await ctx.workspace.changes(workdir)).patch, set(resolved)
            )
        finally:
            await ctx.workspace.release(workdir)
        if left:
            return PlaybookResult(
                failed="the agent left conflicts in " + ", ".join(left),
                result={"left": left, "summary": answer.text.strip()[-1000:]},
            )
        account = answer.text.strip()[-3000:] or f"Resolves the conflicts with {base}."
        rationale = (
            f"merge {base} at {short_sha(attempt.merge_sha)} into "
            f"{short_sha(pr.head_sha)}, resolving {len(resolved)} conflicted files"
        )
        if service_pr:
            action = make_action(
                ActionKind.CODE_UPDATE,
                pr.repo,
                CodeUpdatePayload(
                    base_ref=base,
                    title=f"chore(self): merge {base} into #{pr.number}",
                    body=account,
                    patch=shown,
                    base_sha=pr.head_sha,
                    merge_ref=base,
                    merge_sha=attempt.merge_sha,
                    resolved=resolved,
                    files=sorted(resolved),
                    runtime=answer.runtime,
                    requested_by=by,
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"merge:{event.id}",
                rationale=rationale,
                draft_for=by,
                provenance=Provenance(requested_by=by),
            )
        else:
            action = make_action(
                ActionKind.PR_FIX,
                pr.repo,
                FixPrPayload(
                    title=f"Merge {base} into #{pr.number}",
                    body=account,
                    patch=shown,
                    base_sha=pr.head_sha,
                    files=sorted(resolved),
                    target="branch",
                    merge_ref=base,
                    merge_sha=attempt.merge_sha,
                    resolved=resolved,
                    runtime=answer.runtime,
                    requested_by=by,
                ),
                number=pr.number,
                head_sha=pr.head_sha,
                discriminator=f"merge:{event.id}",
                rationale=rationale,
                draft_for=by,
            )
        return PlaybookResult(
            actions=[action],
            result={"resolved": sorted(resolved), "merge_sha": attempt.merge_sha},
        )

    async def _for_schedule(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        """Resolve a scheduled PR's minor conflicts for the maintainer who scheduled
        it, or refuse, with why; either way, note the outcome and wake its merge job."""
        profile = ctx.require_profile()
        assert event.repo and event.number
        by = str(event.payload["by"])
        ctx.requested_by = by
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or pr.state != PrState.OPEN:
            return PlaybookResult(result={"skipped": "the PR is not open"})
        arm = await ctx.store.get_arm(pr.repo, pr.number)
        if (
            arm is None
            or arm.armed_by.lower() != by.lower()
            or not same_commit(arm.sha, pr.head_sha)
        ):
            return PlaybookResult(
                result={"skipped": f"{by} does not schedule it at this head"}
            )
        plan = profile.merge.schedule.resolve
        wake = Event(
            kind=EventKind.MERGE_QUEUE,
            repo=pr.repo,
            number=pr.number,
            payload={"after": "resolve"},
        )

        async def note(outcome: str, why: str = "", action_id: str = "") -> None:
            await ctx.store.set_state(
                resolve_key(pr.repo, pr.number),
                {
                    "sha": pr.head_sha,
                    "outcome": outcome,
                    "why": why,
                    "action_id": action_id,
                    "at": ctx.clock.now().isoformat(),
                    "job": ctx.job.id,
                },
            )

        async def refuse(why: str) -> PlaybookResult:
            await note("refused", why)
            return PlaybookResult(events=[wake], result={"refused": why})

        if not plan.enabled:
            return await refuse(
                "the repository does not let the schedule resolve conflicts"
            )
        if (
            pr.head_repo not in (None, "", pr.repo)
            and pr.maintainer_can_modify is False
        ):
            return await refuse(
                "its author does not let maintainers push to its branch"
            )
        default_branch = (await ctx.forge.get_repo(pr.repo)).default_branch
        base = pr.base_ref or default_branch
        workdir = await ctx.workspace.prepare(
            pr.repo, pr.number, pr.head_sha, default_branch
        )
        try:
            attempt = await ctx.workspace.merge(workdir, base)
            if not attempt.conflicts:
                await note("clean", f"{base} merges in without conflicts now")
                return PlaybookResult(events=[wake], result={"clean": True})
            if attempt.steering:
                return await refuse(
                    "files that steer coding agents conflict, which no agent "
                    "resolves: " + ", ".join(attempt.steering)
                )
            kinds = {name: kind_of(name, plan.kinds) for name in attempt.conflicts}
            other = [name for name, kind in kinds.items() if kind is None]
            if other:
                return await refuse(
                    ", ".join(other[:5])
                    + (f" and {len(other) - 5} more" if len(other) > 5 else "")
                    + (" is" if len(other) == 1 else " are")
                    + " not of a kind the schedule resolves (enabled: "
                    + ", ".join(k.name for k in plan.kinds if k.enabled)
                    + ")"
                )
            if plan.union_only:
                gone = [
                    name
                    for name in attempt.conflicts
                    if (side := attempt.sides.get(name)) is None
                    or side.ours is None
                    or side.theirs is None
                ]
                if gone:
                    return await refuse(
                        "a side deleted, or the service could not read, "
                        + ", ".join(gone[:5])
                    )
            listing = "\n".join(
                f"- {name}: {kind.name}: {kind.description}"
                for name, kind in kinds.items()
                if kind is not None
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.FIX,
                    prompt="\n\n".join(
                        [
                            RESOLVE_MINOR.format(
                                number=pr.number,
                                base=base,
                                repo=pr.repo,
                                public_language=profile.languages.public,
                            ),
                            "Files with conflicts, and their kind:\n"
                            + untrusted("merge.conflicts", listing, 12_000),
                            pr_block(pr),
                        ]
                    ),
                    workdir=workdir,
                    mode="workspace_write",
                    repo=pr.repo,
                    pr=pr.number,
                    timeout_seconds=1800,
                    activity="merge_resolve",
                )
            )
            if not answer.ok:
                if may_succeed_later(answer):
                    raise RuntimeError(f"the merge agent failed: {answer.error}")
                return await refuse(f"the agent failed: {answer.error}")
            resolved: dict[str, str] = {}
            problems: list[str] = []
            for name in attempt.conflicts:
                text = (Path(workdir) / name).read_text(
                    encoding="utf-8", errors="replace"
                )
                resolved[name] = text
                side = attempt.sides.get(name)
                if unresolved(text):
                    problems.append(f"{name}: left unresolved")
                elif plan.union_only and side is not None:
                    why = union_problem(side.base, side.ours, side.theirs, text)
                    if why:
                        problems.append(f"{name}: {why}")
            shown = only_files(
                (await ctx.workspace.changes(workdir)).patch, set(resolved)
            )
        finally:
            await ctx.workspace.release(workdir)
        account = answer.text.strip()
        if problems:
            return await refuse(
                "; ".join(problems[:5])
                + (f". The agent said: {account[-600:]}" if account else "")
            )
        action = _resolution(
            pr,
            base,
            attempt.merge_sha,
            resolved,
            shown,
            by,
            account,
            answer.runtime,
            kinds,
            armed=int(arm.armed_at.timestamp()),
        )
        await note("proposed", action_id=action.id)
        return PlaybookResult(
            actions=[action],
            events=[wake],
            result={"resolved": sorted(resolved), "merge_sha": attempt.merge_sha},
        )


def _resolution(
    pr: PrSnapshot,
    base: str,
    merge_sha: str,
    resolved: dict[str, str],
    patch: str,
    by: str,
    account: str,
    runtime: str | None,
    kinds: dict,
    armed: int = 0,
) -> Action:
    """The schedule's ``pr.fix`` pushing the resolution onto the PR's branch as the
    maintainer who scheduled it: a merge commit only ``only_base_merged`` keeps the
    schedule through (``Merge <base> into #<n>``)."""
    names = sorted({k.name for k in kinds.values() if k is not None})
    body = (
        (account[-2500:] + "\n\n" if account else "")
        + f"Minor conflicts ({', '.join(names)}) resolved by the merge schedule "
        f"for {by}, who scheduled the PR."
    )
    action = make_action(
        ActionKind.PR_FIX,
        pr.repo,
        FixPrPayload(
            title=f"Merge {base} into #{pr.number}",
            body=body,
            patch=patch,
            base_sha=pr.head_sha,
            files=sorted(resolved),
            target="branch",
            merge_ref=base,
            merge_sha=merge_sha,
            resolved=resolved,
            runtime=runtime,
            requested_by=by,
        ),
        number=pr.number,
        head_sha=pr.head_sha,
        # One per schedule of the PR: scheduled again, it is resolved anew.
        discriminator=f"schedule:{armed}:resolve:{pr.head_sha[:12]}",
        rationale=f"merge {base} at {short_sha(merge_sha)} into "
        f"{short_sha(pr.head_sha)}, resolving minor conflicts in "
        f"{len(resolved)} files; scheduled by {by}",
        draft_for=by,
        provenance=Provenance(requested_by=by, scheduled_by=by),
    )
    return action
