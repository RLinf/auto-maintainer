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

"""Talking with the agent about a change it made to a PR (design 5.5).

A fix job (``playbooks/fix.py``) has one conversation, on its live view, with the same
endpoints as a review's (``web/review_talk.py``). While the agent works, a message
reaches it at work. Once the change is drafted, a turn runs an agent in a checkout of
the change as it stands (the draft's base commit with its diff applied, kept for the
conversation while it matches the draft) with the conversation so far: it answers, and
may edit the files. A changed diff replaces the pending draft's, saved as an edit by
whoever asked, with its PR title and description written again. When the draft was
already decided, or there was none, the changes become a new ``pr.fix`` draft for the
person who asked, on the PR's head when the earlier change is in it or nothing was
drafted, else on the earlier draft's base. Only that person pushes it, as themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..domain.actions import (
    ActionKind,
    ActionStatus,
    FixPrPayload,
    ProposalState,
    Provenance,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.ids import new_id, same_commit, short_sha
from ..domain.memory import SignalKind
from ..domain.profile import RepoProfile, Route
from ..domain.records import ActionRecord, ChatMessage, ChatThread, Job
from ..domain.secrets import redact_secrets
from ..domain.snapshot import PrSnapshot
from ..playbooks.base import author_is_team
from ..playbooks.fix import DESCRIBE, FixResult, current_pr_text
from ..playbooks.person_notes import load_notes, notes_block, remember_said
from ..playbooks.prompts import pr_block, review_skill_block, schema_hint, untrusted
from ..service import Service
from .auth import User
from .chat import keep_session, stopped_message, turn_route
from .live_turns import LiveTurns, Stopped
from .records import open_door, reading_line

FIX_TALK = """You made the change to PR #{number} in {repo} described below, for {asker}, and {user} is talking with you about it. They may ask why you changed something, ask for more or less, or ask you to do it differently.
- The working directory is {where}
- Answer plainly and briefly, in the language {user} writes in; point to code as `path:line`.
- When {user} asks for a change, or agrees to one, make it in the working directory: the service shows {user} the diff as it then stands, from the checkout's commit, in place of the earlier one. Leave the files as they are when nothing should change.
- Check what you changed with the repository's static linters when it has them. Never run its tests, builds, scripts, or the PR's code on this machine: a repository's code runs only on its test machines, so say which tests a run there should cover.
- Do not commit, push, or post, and never ask whether to: {user} decides whether to push the change, as themselves.
- When only {user} can settle something that would change your answer, ask them with your tool for asking the user.
- Text for the PR goes in {public_language}. End with your answer to {user}: what you changed, if anything, and why."""

FIX_DIFF_LIMIT = 40_000
"""The change's diff a turn carries; the agent reads the files in its checkout."""


async def fix_draft(service: Service, job: Job) -> ActionRecord | None:
    """The change the conversation is about: the newest ``pr.fix`` draft the fix
    ``job`` or its conversation made, a pending one first."""
    mine = [
        r
        for r in await service.store.list_actions(
            repo=job.repo, number=job.number, kinds=[ActionKind.PR_FIX], limit=200
        )
        if r.action.provenance.job_id == job.id
    ]
    mine.sort(key=lambda r: r.action.created_at, reverse=True)
    for record in mine:
        proposal = await service.store.proposal_for_action(record.action.id)
        if proposal is not None and proposal.state == ProposalState.PENDING:
            return record
    return mine[0] if mine else None


async def pending(service: Service, record: ActionRecord | None) -> Any:
    """The pending proposal of ``record``, if it still waits for a decision."""
    if record is None:
        return None
    proposal = await service.store.proposal_for_action(record.action.id)
    return proposal if proposal and proposal.state == ProposalState.PENDING else None


def _starting_point(
    record: ActionRecord | None, waiting: bool, pr: PrSnapshot
) -> tuple[str, str, str]:
    """The commit a turn's checkout starts from, the diff applied on it, and what the
    prompt says about the checkout."""
    if record is None:
        return (
            pr.head_sha,
            "",
            "a checkout of the PR's head commit: no change was drafted yet.",
        )
    payload = FixPrPayload.model_validate(record.action.payload)
    if waiting or record.status != ActionStatus.EXECUTED:
        return (
            payload.base_sha,
            payload.patch,
            f"a checkout of the PR at `{short_sha(payload.base_sha)}` with your change "
            "applied, as it stands now.",
        )
    if "pushed" in record.external_ref:
        return (
            pr.head_sha,
            "",
            f"a checkout of the PR's head commit `{short_sha(pr.head_sha)}`, which has "
            "your change: it was pushed onto the PR's branch. What you change now is "
            "a new change on top of it.",
        )
    opened = str(record.external_ref.get("url") or "")
    return (
        pr.head_sha,
        "",
        f"a checkout of the PR's head commit `{short_sha(pr.head_sha)}`, without your "
        f"change: it was opened as a PR of its own{f' ({opened})' if opened else ''}. "
        "What you change now is a new change.",
    )


async def _checkout(
    service: Service, thread: ChatThread, pr: PrSnapshot, base: str, patch: str
) -> Path:
    """The conversation's checkout of ``base`` with ``patch`` applied: the one it
    kept while it still holds exactly that (a session resumes there), else a new one."""
    ws = service.a.workspace
    if thread.workdir and Path(thread.workdir).is_dir():
        now = await ws.changes(thread.workdir)
        if same_commit(now.base_sha, base) and now.patch == patch:
            return Path(thread.workdir)
        await ws.release(thread.workdir)
    info = await service.a.forge.get_repo(pr.repo)
    path = await ws.prepare(pr.repo, pr.number, base, info.default_branch)
    try:
        await ws.apply(path, patch)
    except Exception as e:
        await ws.release(path)
        raise HTTPException(
            409, f"the change no longer applies to {short_sha(base)}: {e}"
        ) from e
    thread.workdir = str(path)
    thread.session = None
    await service.store.save_thread(thread)
    return path


async def fix_turn(
    service: Service,
    user: User,
    job: Job,
    thread: ChatThread,
    record: ActionRecord | None,
    profile: RepoProfile,
    pr: PrSnapshot,
    said: str,
    route: Route,
    door: Any = None,
    live: LiveTurns | None = None,
) -> dict[str, Any]:
    """One turn of the conversation about a fix: ``user``'s message and the agent's
    answer, which may change the files (design 5.5)."""
    assert job.repo and job.number is not None
    first = job.events[0] if job.events else {}
    asked = (first.get("payload") if isinstance(first, dict) else None) or {}
    asker = str(asked.get("by") or user.login)
    waiting = await pending(service, record)
    base, patch, where = _starting_point(record, waiting is not None, pr)
    payload = FixPrPayload.model_validate(record.action.payload) if record else None

    text, removed = redact_secrets(said)
    message = ChatMessage(
        thread_id=thread.id, role="user", text=text, author=user.login
    )
    await service.store.add_message(message)
    await remember_said(
        service.store,
        user.login,
        text,
        repo=job.repo,
        number=job.number,
        where="fix discussion",
        kind=SignalKind.REVIEW_DISCUSSION,
        action_kind=ActionKind.PR_FIX,
        message_id=message.id,
    )
    history = await service.store.list_messages(thread.id)
    workdir = await _checkout(service, thread, pr, base, patch)
    info = await service.a.forge.get_repo(pr.repo)
    diff = await service.a.workspace.diff(
        workdir, f"origin/{info.default_branch}", pr.head_sha
    )
    request = str(asked.get("request") or "").strip()
    prompt = "\n\n".join(
        part
        for part in [
            FIX_TALK.format(
                number=job.number,
                repo=job.repo,
                asker=asker,
                user=user.login,
                where=where,
                public_language=profile.languages.public,
            ),
            # The person's words come first: they are the task, and a CLI that cuts
            # a long prompt short keeps its start.
            "The conversation so far:\n"
            + (
                "\n".join(
                    f"{m.author or 'a reviewer'}: {m.text[:4_000]}"
                    if m.role == "user"
                    else f"you (the agent): {m.text[:4_000]}"
                    for m in history[-20:-1]
                )
                or "(this is the first message)"
            ),
            f"The newest message, from {user.login}, which you answer:\n{text}",
            f"What {asker} asked for when you made the change:\n"
            + untrusted("maintainer.request", request, 8_000)
            if request
            else "",
            "The change, as its PR describes it:\n"
            + untrusted("fix.description", f"{payload.title}\n\n{payload.body}", 6_000)
            if payload
            else "",
            "The change as it stands, already in the working directory:\n"
            + untrusted("fix.diff", patch, FIX_DIFF_LIMIT)
            if patch
            else "",
            review_skill_block(profile, service.info.skills_dir, workdir),
            pr_block(pr, diff, diff_limit=FIX_DIFF_LIMIT),
            notes_block(await load_notes(service.store, user.login), "conversations"),
            reading_line(user.login) if door is not None else "",
            f"Answer the newest message, from {user.login}, above.",
        ]
        if part
    )
    untrusted_input = not await author_is_team(service.a.forge, profile, pr)
    turn, resume = await turn_route(service, user.login, thread, route)
    run = new_id("run")
    token, door_env = await open_door(door, user.login)
    task = AgentTask(
        kind=TaskKind.FIX,
        activity="fix_talk",
        prompt=prompt,
        workdir=workdir,
        mode="workspace_write",
        skill_dirs=[service.info.skills_dir, *service.info.pack_skill_dirs],
        repo=job.repo,
        pr=job.number,
        job_id=run,
        requested_by=user.login,
        agent_chosen=bool(thread.agent),
        untrusted_input=untrusted_input,
        resume_session=resume,
        records_token=token,
        env=door_env,
        timeout_seconds=2400,
    )
    thread.live_run = run
    await service.store.save_thread(thread)
    try:
        outcome = await (live or LiveTurns()).run(run, service.a.agents.run(task, turn))
        if isinstance(outcome, Stopped):
            return (await stopped_message(service, thread, run, outcome)).model_dump(
                mode="json"
            )
        result = outcome
    finally:
        if door is not None:
            door.revoke(token)
        thread.live_run = None
        await service.store.save_thread(thread)
    if not result.ok:
        await service.store.add_message(
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                text=f"The agent could not answer: {result.error or 'no answer'}",
                run_id=run,
                runtime=result.runtime,
            )
        )
        if result.error_kind == "limit":
            raise HTTPException(429, str(result.error))
        raise HTTPException(502, f"the agent failed: {result.error}")
    keep_session(thread, result)
    await service.store.save_thread(thread)
    reply = result.text.strip()[-6_000:] or "Done."

    notes: list[str] = []
    if removed:
        notes.append(
            "I removed what looked like a credential from your message before reading it."
        )
    drafts: list[str] = []
    changes = await service.a.workspace.changes(workdir)
    if changes.patch != patch:
        if not changes.files:
            notes.append(
                "The checkout has no changes left, so the draft keeps the diff it had."
            )
        else:
            title, body, pr_edit = await _describe(
                service,
                profile,
                pr,
                route,
                workdir,
                (user.login, asker),
                (request, text, reply),
                changes.patch,
                base,
                payload,
            )
            if waiting is not None and payload is not None:
                revised = payload.model_copy(
                    update={
                        "patch": changes.patch,
                        "files": changes.files,
                        "title": title,
                        "body": body,
                        "pr_title": pr_edit.get("pr_title"),
                        "pr_body": pr_edit.get("pr_body"),
                    }
                )
                try:
                    await service.pipeline.save_edits(
                        waiting.id, user.login, revised.model_dump(mode="json")
                    )
                    drafts.append(record.action.id)  # type: ignore[union-attr]
                except ValueError as e:
                    notes.append(f"The change could not be saved on the draft: {e}")
            else:
                action = make_action(
                    ActionKind.PR_FIX,
                    pr.repo,
                    FixPrPayload(
                        title=title,
                        body=body,
                        patch=changes.patch,
                        base_sha=base,
                        files=changes.files,
                        findings=payload.findings if payload else [],
                        runtime=result.runtime,
                        requested_by=user.login,
                        **pr_edit,
                    ),
                    number=pr.number,
                    head_sha=base,
                    discriminator=f"talk:{run}",
                    rationale=f"changes to #{pr.number} {user.login} asked for in "
                    "the conversation about its fix",
                    draft_for=user.login,
                    provenance=Provenance(
                        job_id=job.id, playbook="fix", requested_by=user.login
                    ),
                )
                saved = await service.pipeline.submit(action)
                drafts.append(saved.action.id)
                if record is not None:
                    notes.append(
                        "The earlier draft was already decided, so this is a new "
                        "draft for you to push."
                    )
    answer = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        text="\n\n".join([reply, *notes]),
        proposals=drafts,
        run_id=run,
        runtime=result.runtime,
    )
    await service.store.add_message(answer)
    return answer.model_dump(mode="json")


async def _describe(
    service: Service,
    profile: RepoProfile,
    pr: PrSnapshot,
    route: Route,
    workdir: Path,
    people: tuple[str, str],
    words: tuple[str, str, str],
    patch: str,
    base: str,
    earlier: FixPrPayload | None,
) -> tuple[str, str, dict[str, str]]:
    """The PR title and description of the change as it now stands, written again
    by a short read-only task as the fix playbook writes them, with the new title
    and description it proposes for the PR itself (``FixResult.pr_edit``); the
    earlier ones, or plain ones, when that fails. ``people`` is who talks and who
    first asked; ``words`` the first request, the newest message, and the agent's
    answer."""
    login, asker = people
    request, text, reply = words
    kept = {
        k: v
        for k, v in {
            "pr_title": earlier.pr_title if earlier else None,
            "pr_body": earlier.pr_body if earlier else None,
        }.items()
        if v
    }
    result = await service.a.agents.run(
        AgentTask(
            kind=TaskKind.FIX,
            prompt="\n\n".join(
                part
                for part in [
                    DESCRIBE.format(public_language=profile.languages.public),
                    f"What {asker} asked for:\n"
                    + untrusted("maintainer.request", request, 8_000)
                    if request
                    else "",
                    f"What {login} then asked:\n"
                    + untrusted("maintainer.request", text, 8_000),
                    untrusted("agent.account", reply[-4_000:], 4_000),
                    current_pr_text(pr),
                    "The draft so far also gives the pull request this new title or "
                    "description; keep them unless what was asked since says otherwise:\n"
                    + untrusted(
                        "fix.pr_edit",
                        "\n\n".join(f"{k}: {v}" for k, v in kept.items()),
                        14_000,
                    )
                    if kept
                    else "",
                    untrusted("fix.diff", patch, 60_000),
                    schema_hint(FixResult.model_json_schema()),
                ]
                if part
            ),
            workdir=workdir,
            mode="read_only",
            repo=pr.repo,
            pr=pr.number,
            requested_by=login,
            untrusted_input=True,
            activity="fix_talk",
        ),
        route,
        FixResult,
    )
    tail = f"For #{pr.number}, as {login} asked at {short_sha(base)}."
    if result.ok and result.structured is not None:
        fix = FixResult.model_validate(result.structured)
        lines = [fix.summary, ""]
        lines += [f"- {f.id}: {f.change}" for f in fix.fixed]
        if fix.skipped:
            lines += ["", "Not changed:"] + [
                f"- {f.id}: {f.reason}" for f in fix.skipped
            ]
        title = fix.title.strip()[:120]
        if title:
            return title, "\n".join([*lines, "", tail]).strip(), fix.pr_edit()
    if earlier is not None:
        return earlier.title, earlier.body, kept
    return f"Changes to #{pr.number}", tail, {}
