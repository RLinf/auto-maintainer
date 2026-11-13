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

"""One handler per action kind. Handlers are the only code that writes outside the process."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
    ApprovePrPayload,
    ApproveRunsPayload,
    CodeMergePayload,
    CodePrPayload,
    CodeUpdatePayload,
    CommentUpsertPayload,
    FixPrPayload,
    InlineComment,
    IssueCommentPayload,
    IssueLabelPayload,
    LabelPayload,
    LabReproducePayload,
    LabRunPayload,
    MemoryChangePayload,
    MemoryCommitPayload,
    MergePayload,
    NotifyPayload,
    NudgePayload,
    PrEditPayload,
    PrStatePayload,
    RequestReviewersPayload,
    RerunPayload,
    ReviewPostPayload,
    ReviewUpdatePayload,
    ServiceDeployPayload,
    ThreadReplyPayload,
    ThreadResolvePayload,
)
from ..domain.diffs import commentable, fits
from ..domain.ids import same_commit
from ..domain.labs import LabProgress
from ..domain.markers import parse_markers
from ..domain.memory import MemoryChange, MemoryChangeState
from ..domain.ports import ReviewCommentSpec
from ..domain.profile import RepoProfile
from ..domain.records import AuditEntry, ChatMessage, ReviewRecord
from ..domain.snapshot import Mergeable, PrSnapshot
from ..domain.visibility import PublicText
from ..policy.leak_guard import mentioned
from .context import ExecContext
from .memory_gate import try_merge_memory

log = logging.getLogger(__name__)


class Noop(Exception):
    """The action had nothing to do; it is recorded as ``noop``."""


class Stale(Exception):
    """The target moved on; the action is superseded."""


class NotWritten(RuntimeError):
    """The forge refused the action's only write, so nothing was written: the
    proposal goes back to its reviewers with the reason, to change and approve again."""


REFUSED = (400, 404, 409, 422)
"""Statuses with which a forge refuses a request outright, writing nothing."""


def _outside_the_diff(c: InlineComment) -> str:
    """An inline comment the forge would not take, as a part of the main comment:
    its place, then its text."""
    lines = f"{c.start_line}-{c.line}" if c.start_line else str(c.line)
    return f"\n\n---\n\n`{c.path}:{lines}`\n\n{c.body.strip()}"


Handler = Callable[
    [ExecContext, Action, RepoProfile | None, PrSnapshot | None, str | None],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class HandlerSpec:
    run: Handler
    needs_pr: bool = False
    """Re-read the PR before running and refuse if it closed."""
    optional_pr: bool = False
    """With ``needs_pr``: ``action.number`` may be ``None`` (a new PR against the
    default branch); when set, the PR is still re-read and checked."""
    needs_current_head: bool = False
    """Refuse if the PR's head moved past ``action.head_sha``."""
    approved_on_its_commit: bool = False
    """With ``needs_current_head``: run anyway after the head moved when a person
    approved it, on ``action.head_sha``, the commit it was written for."""


async def _labels(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = LabelPayload.model_validate(action.payload)
    wanted = [profile.label_for(r) for r in p.roles] + p.labels
    info = await x.reader.get_repo(action.repo)
    known = set(info.labels)
    unknown = [lbl for lbl in wanted if lbl and known and lbl not in known]
    add = [
        lbl
        for lbl in wanted
        if lbl and (not known or lbl in known) and lbl not in pr.labels
    ]
    removed = []
    for role in p.remove_roles:
        label = profile.label_for(role)
        if (
            label
            and label in pr.labels
            and await _service_added(x, action.repo, pr.number, label)
        ):
            await x.writer.remove_label(action.repo, pr.number, label)
            removed.append(label)
    if add:
        await x.writer.add_labels(action.repo, pr.number, sorted(set(add)))
    if not add and not removed:
        raise Noop(
            "labels already present"
            + (f"; unknown labels skipped: {unknown}" if unknown else "")
        )
    return {"added": sorted(set(add)), "removed": removed, "unknown": unknown}


async def _service_added(x: ExecContext, repo: str, number: int, label: str) -> bool:
    """Whether the service added ``label``; it never removes a label a human added."""
    records = await x.store.list_actions(
        repo=repo,
        number=number,
        kinds=[ActionKind.LABEL_APPLY, ActionKind.LABEL_CI_GATE],
        statuses=[ActionStatus.EXECUTED],
    )
    return any(label in r.external_ref.get("added", []) for r in records)


async def _review_post(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None and action.head_sha
    p = ReviewPostPayload.model_validate(action.payload)
    # The forge refuses a whole review over one comment on a line its diff does not
    # show; such a comment goes in the main comment instead, under its place.
    diff = await x.reader.get_pr_diff(action.repo, pr.number)
    ranges = commentable(diff) if diff else None
    inline = [
        c
        for c in p.comments
        if ranges is None
        or fits(ranges, c.path, c.line, c.start_line, c.side) is not False
    ]
    outside = [c for c in p.comments if c not in inline]
    body = await x.public(
        action,
        profile,
        p.body + "".join(_outside_the_diff(c) for c in outside),
        marker_kind=p.kind,
        pr=pr,
        runtime=p.runtime,
        approver=approver,
    )
    comments = []
    for c in inline[: profile.review.max_inline_comments]:
        text = await x.public(
            action, profile, c.body, marker_kind=p.kind, pr=pr, bare=True
        )
        comments.append(
            ReviewCommentSpec(
                path=c.path,
                line=c.line,
                body=text,
                start_line=c.start_line,
                side=c.side,
            )
        )
    try:
        ref = await x.writer.create_review(
            action.repo, pr.number, action.head_sha, body, p.event, comments
        )
    except Exception as e:
        if getattr(e, "status", None) in REFUSED:
            raise NotWritten(f"the forge refused the review: {e}") from e
        raise
    await x.store.add_review_record(
        ReviewRecord(
            repo=action.repo,
            number=pr.number,
            sha=action.head_sha,
            kind=p.kind,
            action_id=action.id,
            result={**(p.result or {}), "body": p.body},
            external_id=ref.id,
            target="review",
        )
    )
    return {
        "review_id": ref.id,
        "url": ref.url,
        "inline": len(comments),
        "moved_to_body": len(outside),
    }


async def _review_update(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = ReviewUpdatePayload.model_validate(action.payload)
    body = await x.public(
        action,
        profile,
        p.body,
        marker_kind=p.kind,
        pr=pr,
        runtime=p.runtime,
        approver=approver,
    )
    if p.target == "review":
        await x.writer.update_review(action.repo, pr.number, p.target_id, body)
    else:
        await x.writer.update_comment(action.repo, pr.number, p.target_id, body)
    if p.revises:
        # The posted review now says something else: keep what follow-ups, scores,
        # and the conversation read in step with it.
        original = await x.store.get_action(p.revises)
        if original is not None:
            payload = {**original.action.payload, "body": p.body}
            if p.result is not None:
                payload["result"] = p.result
            original.action = original.action.model_copy(update={"payload": payload})
            await x.store.save_action(original)
        await x.store.revise_review_record(
            action.repo,
            pr.number,
            p.revises,
            {**(p.result or {}), "body": p.body},
        )
    return {"target": p.target, "id": p.target_id}


async def _thread_reply(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = ThreadReplyPayload.model_validate(action.payload)
    body = await x.public(
        action, profile, p.body, marker_kind="reply", pr=pr, bare=True
    )
    ref = await x.writer.reply_to_review_comment(
        action.repo, pr.number, p.comment_id, body
    )
    return {"comment_id": ref.id}


async def _thread_resolve(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert pr is not None
    p = ThreadResolvePayload.model_validate(action.payload)
    thread = next((t for t in pr.threads if t.id == p.thread_id), None)
    if thread is None:
        raise Stale(f"the review thread {p.thread_id} is no longer on the PR")
    if thread.is_resolved:
        raise Noop("the review thread is already resolved")
    await x.writer.resolve_review_thread(action.repo, pr.number, p.thread_id)
    return {"thread_id": p.thread_id}


async def _comment_upsert(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = CommentUpsertPayload.model_validate(action.payload)
    body = await x.public(
        action,
        profile,
        p.body,
        marker_kind=p.purpose,
        pr=pr,
        runtime=p.runtime,
        approver=approver,
    )
    existing = None
    for c in pr.comments:
        if c.author.lower() == x.service_login.lower() and any(
            m.kind == p.purpose for m in parse_markers(c.body)
        ):
            existing = c
    if existing is not None:
        if _strip_marker(existing.body) == _strip_marker(body.text):
            raise Noop(f"the {p.purpose} comment is already up to date")
        await x.writer.update_comment(action.repo, pr.number, existing.id, body)
        return {"comment_id": existing.id, "updated": True}
    ref = await x.writer.create_comment(action.repo, pr.number, body)
    return {"comment_id": ref.id, "url": ref.url, "updated": False}


def _strip_marker(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("<!-- auto-maintainer:")
    ).strip()


async def _rerun(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None
    p = RerunPayload.model_validate(action.payload)
    done = await x.store.list_actions(
        repo=action.repo,
        number=action.number,
        kinds=[ActionKind.CI_RERUN_FAILED],
        statuses=[ActionStatus.EXECUTED],
    )
    same = [
        r
        for r in done
        if r.action.payload.get("run_id") == p.run_id
        and r.action.head_sha == action.head_sha
    ]
    if len(same) >= profile.ci.rerun.max_per_sha:
        raise Noop(
            f"rerun budget used: {len(same)} reruns of run {p.run_id} at this SHA"
        )
    await x.ci.rerun_failed_jobs(action.repo, p.run_id)
    return {"run_id": p.run_id, "rerun": len(same) + 1}


async def _request_reviewers(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert pr is not None
    p = RequestReviewersPayload.model_validate(action.payload)
    logins = [
        lg
        for lg in p.logins
        if lg.lower() != pr.author.lower()
        and lg not in pr.requested_reviewers
        and "/" not in lg
    ]
    if not logins:
        raise Noop("every suggested reviewer is already requested or is the author")
    await x.writer.request_reviewers(action.repo, pr.number, logins)
    return {"requested": logins}


async def _approve_runs(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Approve the workflow runs GitHub holds for the PR's head, as the person who
    pressed the button (the executor gives this handler their writer)."""
    p = ApproveRunsPayload.model_validate(action.payload)
    for run_id in p.run_ids:
        await x.writer.approve_workflow_run(action.repo, run_id)
    return {"approved": p.run_ids}


async def _approve_pr(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Approve the PR at the head the person saw, as them: the executor gives this
    handler their own writer, and the service's writer refuses to approve."""
    assert profile is not None and pr is not None
    p = ApprovePrPayload.model_validate(action.payload)
    body = await x.public(
        action, profile, p.body, marker_kind="approve", pr=pr, bare=True
    )
    ref = await x.writer.approve_pr(action.repo, pr.number, pr.head_sha, body)
    return {"review_id": ref.id, "url": ref.url, "sha": pr.head_sha}


async def _update_branch(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert pr is not None
    await x.writer.update_branch(action.repo, pr.number, pr.head_sha)
    return {"from": pr.head_sha}


async def _merge(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None and action.head_sha
    p = MergePayload.model_validate(action.payload)
    if p.queue:
        # The branch takes merges only through its merge queue, which merges the
        # PR with the queue's own message once the queue's checks pass.
        await x.writer.enqueue_merge(action.repo, pr.number, action.head_sha)
        return {"queued": True, "method": p.method}
    subject = await x.public(
        action, profile, p.subject, marker_kind="merge", pr=pr, bare=True
    )
    body = await x.public(
        action, profile, p.body or " ", marker_kind="merge", pr=pr, bare=True
    )
    sha = await x.writer.merge(
        action.repo, pr.number, action.head_sha, p.method, subject, body
    )
    await x.store.delete_arm(action.repo, pr.number)
    return {"merge_sha": sha, "method": p.method}


async def _pr_edit(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Set the PR's title and description, as the person who saved them: the
    executor gives this handler their own writer. Mentions the description already
    had may stay; a new one passes the leak guard like any public text."""
    assert profile is not None and pr is not None
    p = PrEditPayload.model_validate(action.payload)
    edit = await _pr_text(x, action, profile, pr, p.title, p.body)
    if edit is None:
        raise Noop("the title and description are already these")
    await x.writer.update_pr(action.repo, pr.number, *edit)
    return {"title": p.title}


async def _pr_text(
    x: ExecContext,
    action: Action,
    profile: RepoProfile,
    pr: PrSnapshot,
    title: str,
    body: str,
) -> tuple[PublicText, PublicText] | None:
    """A PR's new title and description through the leak guard, or None when they
    are the PR's already. Mentions the PR's title and description already had may
    stay."""
    if title == pr.title and body == pr.body:
        return None
    had = mentioned(f"{pr.title}\n{pr.body}")
    return (
        await x.public(
            action,
            profile,
            title,
            marker_kind="edit",
            pr=pr,
            bare=True,
            also_mention=had,
        ),
        await x.public(
            action,
            profile,
            body or " ",
            marker_kind="edit",
            pr=pr,
            bare=True,
            also_mention=had,
        ),
    )


async def _nudge(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = NudgePayload.model_validate(action.payload)
    body = await x.public(
        action,
        profile,
        p.body,
        marker_kind=f"nudge-{p.audience}",
        pr=pr,
        approver=approver,
    )
    ref = await x.writer.create_comment(action.repo, pr.number, body)
    return {"comment_id": ref.id}


async def _draft_or_close(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and pr is not None
    p = PrStatePayload.model_validate(action.payload)
    if p.body.strip():
        body = await x.public(
            action,
            profile,
            p.body,
            marker_kind=str(action.kind),
            pr=pr,
            approver=approver,
        )
        await x.writer.create_comment(action.repo, pr.number, body)
    if action.kind == ActionKind.PR_CLOSE:
        await x.writer.close(action.repo, pr.number)
    else:
        if pr.is_draft:
            raise Noop("already a draft")
        await x.writer.convert_to_draft(action.repo, pr.number)
    return {}


async def _issue_label(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert action.number is not None
    p = IssueLabelPayload.model_validate(action.payload)
    info = await x.reader.get_repo(action.repo)
    labels = [lbl for lbl in p.labels if not info.labels or lbl in info.labels]
    if not labels:
        raise Noop("no known labels to add")
    await x.writer.add_labels(action.repo, action.number, labels)
    return {"added": labels}


async def _issue_comment(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None and action.number is not None
    p = IssueCommentPayload.model_validate(action.payload)
    body = await x.public(
        action, profile, p.body, marker_kind="issue", pr=None, approver=approver
    )
    ref = await x.writer.create_comment(action.repo, action.number, body)
    return {"comment_id": ref.id}


async def _lab_run(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    assert profile is not None
    if x.labs is None:
        raise RuntimeError("no lab hosts are configured")
    p = LabRunPayload.model_validate(action.payload)
    return await x.labs.run(
        profile, p, action.repo, action.number, action.head_sha, holder=action.id
    )


async def _lab_reproduce(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Open the approved reproduction's session on the test machine: copy the code
    before the PR and at its head over (the machine never gets credentials: the
    service checks the PR out and streams each side's files), and keep the machine
    for the agent, whose every command is checked against the machine's rules
    (``labs/sessions.py``). The reproduce playbook runs the agent and closes it."""
    assert pr is not None
    if x.labs is None or x.workspace is None:
        raise RuntimeError("no test machines are configured")
    p = LabReproducePayload.model_validate(action.payload)
    info = await x.reader.get_repo(action.repo)
    workdir = await x.workspace.prepare(
        action.repo, pr.number, pr.head_sha, info.default_branch
    )
    try:
        base = (
            await x.workspace.merge_base(
                workdir, f"origin/{info.default_branch}", pr.head_sha
            )
            or f"origin/{info.default_branch}"
        )
        refs = {"base": base, "head": pr.head_sha}
        code = {
            side: await x.workspace.archive(workdir, refs[side]) for side in p.sides
        }
    finally:
        await x.workspace.release(workdir)
    job_id = action.provenance.job_id

    async def progress(step: LabProgress) -> None:
        if x.lab_progress is not None and job_id:
            await x.lab_progress(job_id, action.repo, pr.number, p.lab, step)

    out = await x.labs.open_session(
        p,
        action.repo,
        pr.number,
        code,
        holder=action.id,
        binding=profile.labs.get(p.lab) if profile else None,
        progress=progress,
        job=job_id,
    )
    return {**out, "base_sha": base, "head_sha": pr.head_sha}


async def _memory_commit(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    p = MemoryCommitPayload.model_validate(action.payload)
    existing = await x.memory.read_text(p.path) or ""
    text = existing + "".join(line.rstrip("\n") + "\n" for line in p.lines)
    sha = await x.memory.commit_direct({p.path: text}, p.message, "auto-maintainer")
    return {"sha": sha, "lines": len(p.lines)}


async def _memory_change(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    p = MemoryChangePayload.model_validate(action.payload)
    change = await x.store.get_memory_change(p.change_id)
    if change is None:
        raise RuntimeError(f"memory change {p.change_id} does not exist")
    if p.replaces:
        await _close_replaced(x, p.replaces, change)
    change = await try_merge_memory(x, change)
    return {"change_id": change.id, "state": change.state, "branch": change.branch}


async def _close_replaced(x: ExecContext, old_id: str, new: MemoryChange) -> None:
    """Close the open draft ``new`` revises, its staged branch, and its memory PR.
    Only an unapproved draft for the same repositories is closed: one a maintainer
    approved stays for them to decide."""
    old = await x.store.get_memory_change(old_id)
    if (
        old is None
        or old.state != MemoryChangeState.OPEN
        or old.approvals
        or sorted(old.repos) != sorted(new.repos)
    ):
        return
    old.state = MemoryChangeState.REJECTED
    old.rejected_reason = f"replaced by {new.id}"
    await x.store.save_memory_change(old)
    await x.memory.drop_change(old)
    remote = x.memory.remote_repo()
    if remote and old.pr_number:
        try:
            await x.writer.close(remote, old.pr_number)
        except Exception as e:  # the dashboard already shows it closed
            log.warning("could not close memory PR #%s: %s", old.pr_number, e)
    await x.store.add_audit(
        AuditEntry(
            kind="memory",
            summary=f"{old.title}: replaced by {new.id}",
            data={"change": old.id, "replaced_by": new.id},
        )
    )


async def _pr_fix(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Push the change as the approving maintainer (the executor gives this handler
    their writer and publisher; the service's own account never runs it): as a new PR
    or draft PR against the default branch when ``pr`` is ``None``, or straight onto
    an existing PR's branch or to a new branch with a PR into that PR's branch, which
    the PR's author merges. A merge of the base branch goes either way too, for an
    approver who cannot push to the PR's branch (a fork that does not let them).

    A new title or description for the PR (``pr_title``, ``pr_body``) goes onto the
    PR itself either way, after the push, as the approver: they pass the leak guard
    before anything is pushed."""
    assert profile is not None and approver
    p = FixPrPayload.model_validate(action.payload)
    if x.publisher is None:
        raise RuntimeError("this service cannot push branches")
    email = f"{approver}@users.noreply.github.com"
    message = (
        f"{p.title}\n\n{p.body}\n\n"
        f"Drafted by an agent in auto-maintainer; reviewed and pushed by {approver}.\n\n"
        f"Signed-off-by: {approver} <{email}>"
    )
    if pr is None:
        if p.target == "branch":
            raise NotWritten(
                "a change without an open PR opens a PR; it cannot push straight to a branch"
            )
        base_ref = p.base_ref or (await x.reader.get_repo(action.repo)).default_branch
        allowed = {approver, *(await x.code_owners(profile, p.files))}
        body = await x.public(
            action,
            profile,
            p.body,
            marker_kind="fix",
            pr=None,
            runtime=p.runtime,
            also_mention=allowed,
        )
        title = await x.public(
            action,
            profile,
            p.title,
            marker_kind="fix",
            pr=None,
            bare=True,
            also_mention=allowed,
        )
        branch = f"am/pr-{action.id[-6:]}"
        sha = await x.publisher.publish(
            action.repo,
            p.base_sha,
            p.patch,
            branch,
            message,
            author=(approver, email),
            merge_sha=p.merge_sha,
            resolved=p.resolved,
        )
        ref = await x.writer.create_pr(
            action.repo, branch, base_ref, title, body, draft=p.draft
        )
        return {
            "branch": branch,
            "sha": sha,
            "pr": ref.id,
            "url": ref.url,
            **({"draft": True} if p.draft else {}),
        }
    edit = None
    if p.pr_title is not None or p.pr_body is not None:
        edit = await _pr_text(
            x,
            action,
            profile,
            pr,
            (p.pr_title or "").strip() or pr.title,
            pr.body if p.pr_body is None else p.pr_body,
        )
    owner = action.repo.split("/", 1)[0]
    head_repo = pr.head_repo or action.repo
    if p.target == "branch":
        if not same_commit(pr.head_sha, p.base_sha):
            raise Stale(
                f"the PR moved from {p.base_sha[:7]} to {pr.head_sha[:7]} since this "
                "was drafted; ask for it again"
            )
        sha = await x.publisher.publish(
            action.repo,
            p.base_sha,
            p.patch,
            pr.head_ref,
            message,
            author=(approver, email),
            merge_sha=p.merge_sha,
            resolved=p.resolved,
            push_to=head_repo,
        )
        return {
            "pushed": f"{head_repo}:{pr.head_ref}",
            "sha": sha,
            **await _edit_after_push(x, action, pr, edit),
        }
    body = await x.public(
        action, profile, p.body, marker_kind="fix", pr=pr, runtime=p.runtime
    )
    title = await x.public(
        action, profile, p.title, marker_kind="fix", pr=pr, bare=True
    )
    branch = f"am/{'merge' if p.merge_ref else 'fix'}-{pr.number}-{action.id[-6:]}"
    sha = await x.publisher.publish(
        action.repo,
        p.base_sha,
        p.patch,
        branch,
        message,
        author=(approver, email),
        merge_sha=p.merge_sha,
        resolved=p.resolved,
    )
    same_repo = pr.head_repo in (None, "", action.repo)
    ref = await x.writer.create_pr(
        action.repo if same_repo else str(pr.head_repo),
        branch if same_repo else f"{owner}:{branch}",
        pr.head_ref,
        title,
        body,
        draft=p.draft,
    )
    return {
        "branch": branch,
        "sha": sha,
        "pr": ref.id,
        "url": ref.url,
        **({"draft": True} if p.draft else {}),
        **await _edit_after_push(x, action, pr, edit),
    }


async def _edit_after_push(
    x: ExecContext,
    action: Action,
    pr: PrSnapshot,
    edit: tuple[PublicText, PublicText] | None,
) -> dict[str, Any]:
    """Set the PR's title and description once its fix is pushed. A refusal is
    recorded, not raised: the push is done, and a retry must not push again."""
    if edit is None:
        return {}
    try:
        await x.writer.update_pr(action.repo, pr.number, *edit)
    except Exception as e:
        log.warning("could not edit %s#%s after its fix: %s", action.repo, pr.number, e)
        return {"edit_error": str(e)[:500]}
    return {"edited": True}


async def _code_pr(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Open a PR on the service's own repository, pushing the patch first when one is given.

    Opening the PR changes nothing the service runs; a maintainer of that repository
    still has to approve the merge and the deploy.
    """
    if not x.code_repo:
        raise RuntimeError("no code repo is configured")
    p = CodePrPayload.model_validate(action.payload)
    commit = None
    if p.patch:
        if x.publisher is None:
            raise RuntimeError("no branch publisher is configured")
        trailers = f"\n\nRequested-by: {p.requested_by}" if p.requested_by else ""
        commit = await x.publisher.publish(
            x.code_repo, p.base_sha, p.patch, p.branch, f"{p.title}{trailers}\n"
        )
    ref = await x.writer.create_service_pr(
        x.code_repo, p.branch, x.code_branch, p.title, p.body
    )
    if p.thread_id:
        await x.store.add_message(
            ChatMessage(
                thread_id=p.thread_id,
                role="assistant",
                text=(
                    f"I opened {ref.url or f'{x.code_repo}#{ref.id}'} with the change. "
                    "The owner of this service has to approve it before it is merged, "
                    "and again before the service runs it (Service page)."
                ),
            )
        )
    return {"pr": ref.id, "url": ref.url, "commit": commit}


async def _code_update(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    """Bring the base branch into a self-improvement PR on ``code_repo``: through the
    forge when clean, or by pushing the approved merge resolution onto ``am/self/*``."""
    assert pr is not None
    if not x.code_repo or action.repo != x.code_repo:
        raise RuntimeError("no code repo is configured")
    if not pr.head_ref.startswith("am/self/") or pr.head_repo not in (
        None,
        "",
        x.code_repo,
    ):
        raise RuntimeError(
            f"{x.code_repo}#{pr.number} is not a self-improvement PR on an am/self/* branch"
        )
    p = CodeUpdatePayload.model_validate(action.payload)
    by = approver or p.approved_by or p.requested_by
    if p.merge_sha:
        if x.publisher is None:
            raise RuntimeError("no branch publisher is configured")
        if not same_commit(pr.head_sha, p.base_sha):
            raise Stale(
                f"the PR moved from {p.base_sha[:7]} to {pr.head_sha[:7]} since this "
                "was drafted; ask for it again"
            )
        title = (
            p.title
            or f"chore(self): merge {p.merge_ref or pr.base_ref} into #{pr.number}"
        )
        body = f"\n\n{p.body.strip()}" if p.body.strip() else ""
        trailers = f"\n\nRequested-by: {by}" if by else ""
        sha = await x.publisher.publish(
            x.code_repo,
            p.base_sha,
            "",
            pr.head_ref,
            f"{title}{body}{trailers}\n",
            merge_sha=p.merge_sha,
            resolved=p.resolved,
        )
        return {"pushed": f"{x.code_repo}:{pr.head_ref}", "sha": sha, "approved_by": by}
    await x.writer.update_branch(x.code_repo, pr.number, pr.head_sha)
    return {"from": pr.head_sha, "approved_by": by}


async def _code_merge(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    if not x.code_repo:
        raise RuntimeError("no code repo is configured")
    p = CodeMergePayload.model_validate(action.payload)
    current = pr or await x.reader.get_pr(x.code_repo, p.number)
    if current is not None and current.mergeable == Mergeable.CONFLICTING:
        raise NotWritten(
            f"{x.code_repo}#{p.number} conflicts with {current.base_ref}; "
            f"resolve conflicts with {current.base_ref} first"
        )
    sha = await x.writer.merge_service_pr(x.code_repo, p.number, p.method)
    return {"sha": sha, "approved_by": p.approved_by}


async def _service_deploy(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    if x.updater is None:
        raise RuntimeError(
            "this service does not run from a git checkout it can update"
        )
    p = ServiceDeployPayload.model_validate(action.payload)
    result = await x.updater.deploy(p.sha)
    if not result.ok:
        raise RuntimeError(f"deploying {p.sha[:12]} stopped: {result.log[-1500:]}")
    if x.request_restart is not None:
        x.request_restart()
    return {
        "sha": result.sha,
        "previous_sha": result.previous_sha,
        "approved_by": p.approved_by,
    }


async def _notify(
    x: ExecContext,
    action: Action,
    profile: RepoProfile | None,
    pr: PrSnapshot | None,
    approver: str | None,
) -> dict[str, Any]:
    p = NotifyPayload.model_validate(action.payload)
    channel = (
        p.channel
        or (profile.notify.get(p.event) if profile else None)
        or (profile.notify.get("default") if profile else None)
    )
    if not channel:
        raise Noop(f"no channel is configured for {p.event}")
    await x.notifier.send(channel, p.text, p.event)
    return {"channel": channel}


HANDLERS: dict[ActionKind, HandlerSpec] = {
    ActionKind.LABEL_APPLY: HandlerSpec(_labels, needs_pr=True),
    ActionKind.LABEL_CI_GATE: HandlerSpec(
        _labels, needs_pr=True, needs_current_head=True
    ),
    # A reviewer who read the draft may post it after a push: GitHub shows it
    # against the commit it reviewed, and marks inline comments outdated where the
    # code changed since.
    ActionKind.REVIEW_POST: HandlerSpec(
        _review_post,
        needs_pr=True,
        needs_current_head=True,
        approved_on_its_commit=True,
    ),
    # An edited review still covers the commit it reviewed, whatever came since.
    ActionKind.REVIEW_UPDATE: HandlerSpec(_review_update, needs_pr=True),
    ActionKind.THREAD_REPLY: HandlerSpec(_thread_reply, needs_pr=True),
    ActionKind.THREAD_RESOLVE: HandlerSpec(_thread_resolve, needs_pr=True),
    ActionKind.COMMENT_UPSERT: HandlerSpec(_comment_upsert, needs_pr=True),
    ActionKind.CI_RERUN_FAILED: HandlerSpec(
        _rerun, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_REQUEST_REVIEWERS: HandlerSpec(_request_reviewers, needs_pr=True),
    ActionKind.CI_APPROVE_RUN: HandlerSpec(
        _approve_runs, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_APPROVE: HandlerSpec(
        _approve_pr, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_UPDATE_BRANCH: HandlerSpec(
        _update_branch, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_MERGE: HandlerSpec(_merge, needs_pr=True, needs_current_head=True),
    ActionKind.PR_NUDGE: HandlerSpec(_nudge, needs_pr=True),
    ActionKind.PR_CONVERT_TO_DRAFT: HandlerSpec(
        _draft_or_close, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_CLOSE: HandlerSpec(_draft_or_close, needs_pr=True),
    ActionKind.PR_EDIT: HandlerSpec(_pr_edit, needs_pr=True),
    ActionKind.ISSUE_LABEL: HandlerSpec(_issue_label),
    ActionKind.ISSUE_COMMENT: HandlerSpec(_issue_comment),
    ActionKind.LAB_RUN: HandlerSpec(_lab_run, needs_pr=True, needs_current_head=True),
    ActionKind.LAB_REPRODUCE: HandlerSpec(
        _lab_reproduce, needs_pr=True, needs_current_head=True
    ),
    ActionKind.LAB_REVIEW: HandlerSpec(
        _lab_reproduce, needs_pr=True, needs_current_head=True
    ),
    ActionKind.MEMORY_COMMIT: HandlerSpec(_memory_commit),
    ActionKind.MEMORY_CHANGE: HandlerSpec(_memory_change),
    ActionKind.PR_FIX: HandlerSpec(
        _pr_fix, needs_pr=True, optional_pr=True, needs_current_head=True
    ),
    ActionKind.CODE_PR: HandlerSpec(_code_pr),
    ActionKind.CODE_UPDATE: HandlerSpec(
        _code_update, needs_pr=True, needs_current_head=True
    ),
    ActionKind.CODE_MERGE: HandlerSpec(_code_merge),
    ActionKind.SERVICE_DEPLOY: HandlerSpec(_service_deploy),
    ActionKind.NOTIFY: HandlerSpec(_notify),
}

assert set(HANDLERS) == set(ActionKind), "every action kind needs a handler"
