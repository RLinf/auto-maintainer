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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
    CodeMergePayload,
    CodePrPayload,
    CommentUpsertPayload,
    IssueCommentPayload,
    IssueLabelPayload,
    LabelPayload,
    LabRunPayload,
    MemoryChangePayload,
    MemoryCommitPayload,
    MergePayload,
    NotifyPayload,
    NudgePayload,
    PrStatePayload,
    RequestReviewersPayload,
    RerunPayload,
    ReviewPostPayload,
    ReviewUpdatePayload,
    ServiceDeployPayload,
    ThreadReplyPayload,
)
from ..domain.markers import parse_markers
from ..domain.ports import ReviewCommentSpec
from ..domain.profile import RepoProfile
from ..domain.records import ChatMessage, ReviewRecord
from ..domain.snapshot import PrSnapshot
from .context import ExecContext
from .memory_gate import try_merge_memory


class Noop(Exception):
    """The action had nothing to do; it is recorded as ``noop``."""


class Stale(Exception):
    """The target moved on; the action is superseded."""


Handler = Callable[
    [ExecContext, Action, RepoProfile | None, PrSnapshot | None, str | None],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class HandlerSpec:
    run: Handler
    needs_pr: bool = False
    """Re-read the PR before running and refuse if it closed."""
    needs_current_head: bool = False
    """Refuse if the PR's head moved past ``action.head_sha``."""


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
    body = await x.public(
        action,
        profile,
        p.body,
        marker_kind=p.kind,
        pr=pr,
        runtime=p.runtime,
        approver=approver,
    )
    comments = []
    for c in p.comments[: profile.review.max_inline_comments]:
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
    ref = await x.writer.create_review(
        action.repo, pr.number, action.head_sha, body, p.event, comments
    )
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
    return {"review_id": ref.id, "url": ref.url, "inline": len(comments)}


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
        marker_kind="followup",
        pr=pr,
        runtime=p.runtime,
        approver=approver,
    )
    if p.target == "review":
        await x.writer.update_review(action.repo, pr.number, p.target_id, body)
    else:
        await x.writer.update_comment(action.repo, p.target_id, body)
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
        await x.writer.update_comment(action.repo, existing.id, body)
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
    change = await try_merge_memory(x, change)
    return {"change_id": change.id, "state": change.state, "branch": change.branch}


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
                    f"A maintainer of {x.code_repo} has to approve it before it is merged, "
                    "and again before the service runs it (Service page)."
                ),
            )
        )
    return {"pr": ref.id, "url": ref.url, "commit": commit}


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
    ActionKind.REVIEW_POST: HandlerSpec(
        _review_post, needs_pr=True, needs_current_head=True
    ),
    ActionKind.REVIEW_UPDATE: HandlerSpec(
        _review_update, needs_pr=True, needs_current_head=True
    ),
    ActionKind.THREAD_REPLY: HandlerSpec(_thread_reply, needs_pr=True),
    ActionKind.COMMENT_UPSERT: HandlerSpec(_comment_upsert, needs_pr=True),
    ActionKind.CI_RERUN_FAILED: HandlerSpec(
        _rerun, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_REQUEST_REVIEWERS: HandlerSpec(_request_reviewers, needs_pr=True),
    ActionKind.PR_UPDATE_BRANCH: HandlerSpec(
        _update_branch, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_MERGE: HandlerSpec(_merge, needs_pr=True, needs_current_head=True),
    ActionKind.PR_NUDGE: HandlerSpec(_nudge, needs_pr=True),
    ActionKind.PR_CONVERT_TO_DRAFT: HandlerSpec(
        _draft_or_close, needs_pr=True, needs_current_head=True
    ),
    ActionKind.PR_CLOSE: HandlerSpec(_draft_or_close, needs_pr=True),
    ActionKind.ISSUE_LABEL: HandlerSpec(_issue_label),
    ActionKind.ISSUE_COMMENT: HandlerSpec(_issue_comment),
    ActionKind.LAB_RUN: HandlerSpec(_lab_run, needs_pr=True, needs_current_head=True),
    ActionKind.MEMORY_COMMIT: HandlerSpec(_memory_commit),
    ActionKind.MEMORY_CHANGE: HandlerSpec(_memory_change),
    ActionKind.CODE_PR: HandlerSpec(_code_pr),
    ActionKind.CODE_MERGE: HandlerSpec(_code_merge),
    ActionKind.SERVICE_DEPLOY: HandlerSpec(_service_deploy),
    ActionKind.NOTIFY: HandlerSpec(_notify),
}

assert set(HANDLERS) == set(ActionKind), "every action kind needs a handler"
