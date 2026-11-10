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

"""In-memory fakes of the ports, for unit tests, replay evals, and ``am serve --dev``.

``FakeForge`` keeps PR snapshots and applies writes to them, so a poller sees the
service's own labels and comments on its next read.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ..domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    RuntimeHealth,
)
from ..domain.clock import utc_now
from ..domain.memory import MemoryChange
from ..domain.permissions import Permission
from ..domain.ports import (
    CommandResult,
    DeployResult,
    GpuStatus,
    MergeAttempt,
    ReviewCommentSpec,
    UpdateStatus,
    WorkChanges,
)
from ..domain.profile import Route
from ..domain.settings import (
    SECRET_NAME,
    ProviderBinding,
    RuntimeConfig,
    SecretInfo,
    UserToken,
)
from ..domain.snapshot import (
    CommentRef,
    IssueComment,
    IssueSnapshot,
    MergedPrSummary,
    PrSnapshot,
    PrState,
    RepoInfo,
    Review,
    ReviewEventState,
    ReviewThread,
    RunnerInfo,
    ThreadComment,
)
from ..domain.visibility import PublicText


class FakeForge:
    """Implements ``ForgeReader``, ``ForgeWriter``, and ``CiProvider`` in memory."""

    def __init__(self, login: str = "auto-maintainer[bot]") -> None:
        self.login = login
        self.acting_as: str | None = None
        """The person an ``ActingAs`` writer is writing as, while it does."""
        self.repos: dict[str, RepoInfo] = {}
        self.prs: dict[tuple[str, int], PrSnapshot] = {}
        self.issues: dict[tuple[str, int], IssueSnapshot] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.diffs: dict[tuple[str, int], str] = {}
        """PR diffs by (repo, number); without one, a diff is made from the files."""
        self.permissions: dict[tuple[str, str], Permission] = {}
        self.teams: dict[str, set[str]] = {}
        self.path_changers: dict[tuple[str, str], str] = {}
        self.job_logs: dict[int, str] = {}
        self.merged: list[MergedPrSummary] = []
        self.runners: list[RunnerInfo] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._ids = itertools.count(1000)
        self.fail_next: dict[str, Exception] = {}

    # -- setup helpers ---------------------------------------------------------------

    def add_repo(self, info: RepoInfo) -> None:
        self.repos[info.name] = info

    def put_pr(self, pr: PrSnapshot) -> None:
        self.prs[(pr.repo, pr.number)] = pr

    def set_permission(
        self, repo: str, login: str, permission: Permission | str
    ) -> None:
        self.permissions[(repo, login.lower())] = Permission(permission)

    def put_file(self, repo: str, path: str, text: str) -> None:
        self.files[(repo, path)] = text

    def _record(self, _op: str, /, **kwargs: Any) -> None:
        if _op in self.fail_next:
            raise self.fail_next.pop(_op)
        self.writes.append((_op, kwargs))

    def writes_of(self, method: str) -> list[dict[str, Any]]:
        return [w for m, w in self.writes if m == method]

    def _update_pr(self, repo: str, number: int, **changes: Any) -> None:
        pr = self.prs.get((repo, number))
        if pr is not None:
            self.prs[(repo, number)] = pr.model_copy(
                update={**changes, "updated_at": utc_now()}
            )

    # -- ForgeReader -------------------------------------------------------------------

    async def service_login(self) -> str:
        return self.login

    async def get_repo(self, repo: str) -> RepoInfo:
        # Like GitHub, names match whatever their letter case.
        found = self.repos.get(repo) or next(
            (info for name, info in self.repos.items() if name.lower() == repo.lower()),
            None,
        )
        return found or RepoInfo(name=repo)

    async def list_open_prs(self, repo: str) -> list[PrSnapshot]:
        return [
            p
            for (r, _), p in sorted(self.prs.items())
            if r == repo and p.state == PrState.OPEN
        ]

    async def get_pr(self, repo: str, number: int) -> PrSnapshot | None:
        return self.prs.get((repo, number))

    async def list_open_issues(self, repo: str) -> list[IssueSnapshot]:
        return [i for (r, _), i in sorted(self.issues.items()) if r == repo]

    async def list_merged_prs(
        self, repo: str, limit: int = 50
    ) -> list[MergedPrSummary]:
        return self.merged[:limit]

    async def get_file(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self.files.get((repo, path))

    async def get_pr_diff(self, repo: str, number: int) -> str | None:
        if (repo, number) in self.diffs:
            return self.diffs[(repo, number)]
        pr = self.prs.get((repo, number))
        if pr is None:
            return None
        return "".join(
            f"diff --git a/{f.path} b/{f.path}\n--- a/{f.path}\n+++ b/{f.path}\n"
            f"@@ -1,{f.deletions} +1,{f.additions} @@\n"
            + "".join(f"+line {i + 1}\n" for i in range(f.additions))
            for f in pr.files
        )

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names = {
            p[len(prefix) :].split("/")[0]
            for (r, p) in self.files
            if r == repo and p.startswith(prefix)
        }
        return sorted(names)

    async def get_permission(self, repo: str, login: str) -> Permission:
        return self.permissions.get((repo, login.lower()), Permission.NONE)

    async def is_team_member(self, repo: str, team: str, login: str) -> bool:
        return login in self.teams.get(team, set())

    async def path_changed_by(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self.path_changers.get((repo, path))

    async def default_branch_sha(self, repo: str) -> str:
        return (
            self.repos.get(repo) or RepoInfo(name=repo)
        ).default_branch_sha or "0" * 40

    # -- ForgeWriter ---------------------------------------------------------------------

    @staticmethod
    def _text(body: PublicText) -> str:
        if not isinstance(body, PublicText):
            raise TypeError(f"expected PublicText, got {type(body).__name__}")
        return body.text

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._record("add_labels", repo=repo, number=number, labels=labels)
        pr = self.prs.get((repo, number))
        if pr:
            self._update_pr(repo, number, labels=sorted(set(pr.labels) | set(labels)))

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        self._record("remove_label", repo=repo, number=number, label=label)
        pr = self.prs.get((repo, number))
        if pr:
            self._update_pr(repo, number, labels=[x for x in pr.labels if x != label])

    async def create_comment(
        self, repo: str, number: int, body: PublicText
    ) -> CommentRef:
        text = self._text(body)
        cid = next(self._ids)
        self._record("create_comment", repo=repo, number=number, body=text, id=cid)
        pr = self.prs.get((repo, number))
        if pr:
            comment = IssueComment(
                id=cid,
                author=self.login,
                body=text,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            self._update_pr(repo, number, comments=[*pr.comments, comment])
        return CommentRef(
            id=cid, url=f"https://example.test/{repo}/pull/{number}#issuecomment-{cid}"
        )

    async def update_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> None:
        text = self._text(body)
        self._record("update_comment", repo=repo, comment_id=comment_id, body=text)
        for (r, n), pr in list(self.prs.items()):
            if r != repo:
                continue
            comments = [
                c.model_copy(update={"body": text, "updated_at": utc_now()})
                if c.id == comment_id
                else c
                for c in pr.comments
            ]
            if comments != pr.comments:
                self._update_pr(r, n, comments=comments)

    async def delete_comment(self, repo: str, number: int, comment_id: int) -> None:
        self._record("delete_comment", repo=repo, comment_id=comment_id)

    async def create_review(
        self,
        repo: str,
        number: int,
        commit_sha: str,
        body: PublicText,
        event: Literal["COMMENT", "REQUEST_CHANGES"],
        comments: list[ReviewCommentSpec],
    ) -> CommentRef:
        text = self._text(body)
        rid = next(self._ids)
        inline = [
            {"path": c.path, "line": c.line, "body": self._text(c.body)}
            for c in comments
        ]
        self._record(
            "create_review",
            repo=repo,
            number=number,
            commit_sha=commit_sha,
            body=text,
            event=event,
            comments=inline,
            id=rid,
        )
        pr = self.prs.get((repo, number))
        if pr:
            review = Review(
                id=rid,
                author=self.login,
                state=ReviewEventState.COMMENTED
                if event == "COMMENT"
                else ReviewEventState.CHANGES_REQUESTED,
                body=text,
                submitted_at=utc_now(),
                commit_sha=commit_sha,
            )
            threads = [
                ReviewThread(
                    id=f"T{rid}_{i}",
                    path=c["path"],
                    line=c["line"],
                    comments=[
                        ThreadComment(
                            id=next(self._ids),
                            author=self.login,
                            body=c["body"],
                            commit_sha=commit_sha,
                        )
                    ],
                )
                for i, c in enumerate(inline)
            ]
            self._update_pr(
                repo,
                number,
                reviews=[*pr.reviews, review],
                threads=[*pr.threads, *threads],
            )
        return CommentRef(
            id=rid,
            url=f"https://example.test/{repo}/pull/{number}#pullrequestreview-{rid}",
        )

    async def approve_workflow_run(self, repo: str, run_id: int) -> None:
        self._record(
            "approve_workflow_run", repo=repo, run_id=run_id, by=self.acting_as
        )

    async def approve_pr(
        self, repo: str, number: int, commit_sha: str, body: PublicText
    ) -> CommentRef:
        if self.acting_as is None:
            raise PermissionError("the service's own account never approves a PR")
        rid = next(self._ids)
        text = self._text(body)
        self._record(
            "approve_pr",
            repo=repo,
            number=number,
            commit_sha=commit_sha,
            body=text,
            id=rid,
        )
        pr = self.prs.get((repo, number))
        if pr:
            review = Review(
                id=rid,
                author=self.login,
                state=ReviewEventState.APPROVED,
                body=text,
                submitted_at=utc_now(),
                commit_sha=commit_sha,
            )
            self._update_pr(repo, number, reviews=[*pr.reviews, review])
        return CommentRef(
            id=rid,
            url=f"https://example.test/{repo}/pull/{number}#pullrequestreview-{rid}",
        )

    async def update_review(
        self, repo: str, number: int, review_id: int, body: PublicText
    ) -> None:
        self._record(
            "update_review",
            repo=repo,
            number=number,
            review_id=review_id,
            body=self._text(body),
        )

    async def resolve_review_thread(
        self, repo: str, number: int, thread_id: str
    ) -> None:
        self._record(
            "resolve_review_thread", repo=repo, number=number, thread_id=thread_id
        )
        pr = self.prs.get((repo, number))
        if pr is not None:
            threads = [
                t.model_copy(update={"is_resolved": True}) if t.id == thread_id else t
                for t in pr.threads
            ]
            self.prs[(repo, number)] = pr.model_copy(update={"threads": threads})

    async def reply_to_review_comment(
        self, repo: str, number: int, comment_id: int, body: PublicText
    ) -> CommentRef:
        cid = next(self._ids)
        self._record(
            "reply_to_review_comment",
            repo=repo,
            number=number,
            comment_id=comment_id,
            body=self._text(body),
            id=cid,
        )
        return CommentRef(id=cid)

    async def request_reviewers(
        self, repo: str, number: int, logins: list[str]
    ) -> None:
        self._record("request_reviewers", repo=repo, number=number, logins=logins)

    async def update_branch(
        self, repo: str, number: int, expected_head_sha: str
    ) -> None:
        self._record(
            "update_branch",
            repo=repo,
            number=number,
            expected_head_sha=expected_head_sha,
            author=self.login,
        )

    async def merge(
        self,
        repo: str,
        number: int,
        sha: str,
        method: str,
        subject: PublicText,
        body: PublicText,
    ) -> str:
        self._record(
            "merge",
            repo=repo,
            number=number,
            sha=sha,
            method=method,
            subject=self._text(subject),
            body=self._text(body),
        )
        self._update_pr(repo, number, state=PrState.MERGED)
        return "m" * 40

    async def convert_to_draft(self, repo: str, number: int) -> None:
        self._record("convert_to_draft", repo=repo, number=number)
        self._update_pr(repo, number, is_draft=True)

    async def close(self, repo: str, number: int) -> None:
        self._record("close", repo=repo, number=number)
        self._update_pr(repo, number, state=PrState.CLOSED)

    async def create_service_issue(
        self, repo: str, title: str, body: str
    ) -> CommentRef:
        n = next(self._ids)
        self._record(
            "create_service_issue", repo=repo, title=title, body=body, number=n
        )
        return CommentRef(id=n)

    async def create_pr(
        self, repo: str, head: str, base: str, title: PublicText, body: PublicText
    ) -> CommentRef:
        n = next(self._ids)
        self._record(
            "create_pr",
            repo=repo,
            head=head,
            base=base,
            title=self._text(title),
            body=self._text(body),
            number=n,
            author=self.login,
        )
        return CommentRef(id=n)

    async def create_service_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> CommentRef:
        n = next(self._ids)
        self._record(
            "create_service_pr",
            repo=repo,
            head=head,
            base=base,
            title=title,
            body=body,
            number=n,
        )
        return CommentRef(id=n)

    async def merge_service_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> str:
        self._record("merge_service_pr", repo=repo, number=number, method=method)
        return "s" * 40

    # -- CiProvider ---------------------------------------------------------------------

    async def get_job_log(self, repo: str, job_id: int) -> str | None:
        return self.job_logs.get(job_id)

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        self._record("rerun_failed_jobs", repo=repo, run_id=run_id)

    async def list_runners(self, repo: str) -> list[RunnerInfo]:
        return self.runners


class FakeMemory:
    """Implements ``MemoryStore`` with a dict; branches are kept per change."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.branches: dict[str, dict[str, str | None]] = {}
        self.commits: list[str] = []
        self._n = itertools.count(1)
        self._head = "mem0"

    def _bump(self, message: str) -> str:
        self._head = f"mem{next(self._n)}"
        self.commits.append(message)
        return self._head

    async def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    async def list_files(self, prefix: str) -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix))

    async def head(self) -> str:
        return self._head

    async def refresh(self) -> bool:
        return False

    async def commit_direct(
        self, files: dict[str, str | None], message: str, author: str
    ) -> str:
        for path, text in files.items():
            if text is None:
                self.files.pop(path, None)
            else:
                self.files[path] = text
        return self._bump(message)

    async def stage_change(self, change: MemoryChange) -> str:
        branch = f"am/mem/{change.id}"
        self.branches[branch] = dict(change.files)
        return branch

    async def merge_change(self, change: MemoryChange) -> str:
        files = self.branches.pop(change.branch or f"am/mem/{change.id}", change.files)
        return await self.commit_direct(files, change.title, change.author)

    async def drop_change(self, change: MemoryChange) -> None:
        self.branches.pop(change.branch or "", None)

    def remote_repo(self) -> str | None:
        return None


class ScriptedRunner:
    """Implements ``AgentRunner``: answers each task kind from a queue or a function."""

    def __init__(self) -> None:
        self.answers: dict[str, list[Any]] = {}
        self.handlers: dict[str, Callable[[AgentTask], Any]] = {}
        self.tasks: list[AgentTask] = []
        self.routes: list[Route] = []
        """The route each task ran on, in order."""
        self.steps: dict[str, Any] = {}
        """Events each task kind reports while it "works", for the live view: a list,
        or a function of the task that returns one."""
        self.step_delay = 0.0
        """Seconds between those events; the demo sets it so the view moves."""
        self.progress: Any = None
        self.listening_jobs: dict[str, int] = {}
        """Jobs with a task running now that takes reviewers' messages."""
        self.told: list[tuple[str, str, str]] = []
        """Messages delivered to running tasks: job, reviewer, text."""

    def queue(
        self, kind: str, answer: dict[str, Any] | BaseModel | AgentResult
    ) -> None:
        self.answers.setdefault(kind, []).append(answer)

    def on(self, kind: str, handler: Callable[[AgentTask], Any]) -> None:
        self.handlers[kind] = handler

    def bind_progress(self, observer: Any) -> None:
        self.progress = observer

    async def run(
        self, task: AgentTask, route: Route, output_model: type[BaseModel] | None = None
    ) -> AgentResult:
        result = await self._answer(task, route, output_model)
        if self.progress is not None:
            await self.progress(
                task,
                result.runtime,
                AgentEvent(type=AgentEventType.FINAL, result=result),
            )
        return result

    def tell(self, job_id: str, by: str, text: str) -> bool:
        if not self.listening(job_id):
            return False
        self.told.append((job_id, by, text))
        return True

    def listening(self, job_id: str) -> bool:
        return self.listening_jobs.get(job_id, 0) > 0

    async def _answer(
        self, task: AgentTask, route: Route, output_model: type[BaseModel] | None
    ) -> AgentResult:
        listens = task.messages and task.job_id is not None
        if listens:
            assert task.job_id is not None
            self.listening_jobs[task.job_id] = (
                self.listening_jobs.get(task.job_id, 0) + 1
            )
        try:
            return await self._scripted(task, route, output_model)
        finally:
            if listens:
                assert task.job_id is not None
                self.listening_jobs[task.job_id] -= 1

    async def _scripted(
        self, task: AgentTask, route: Route, output_model: type[BaseModel] | None
    ) -> AgentResult:
        self.tasks.append(task)
        self.routes.append(route)
        kind = str(task.kind)
        if self.progress is not None:
            await self.progress(
                task, route.primary, AgentEvent(type=AgentEventType.STARTED)
            )
            steps = self.steps.get(kind, [])
            for event in steps(task) if callable(steps) else steps:
                if self.step_delay:
                    await asyncio.sleep(self.step_delay)
                await self.progress(task, route.primary, event)
        if kind in self.handlers:
            answer = self.handlers[kind](task)
        elif self.answers.get(kind):
            answer = self.answers[kind].pop(0)
        else:
            return AgentResult(
                runtime="scripted",
                ok=False,
                error=f"no scripted answer for {kind}",
                error_kind="other",
            )
        if isinstance(answer, AgentResult):
            return answer
        data = (
            answer.model_dump(mode="json") if isinstance(answer, BaseModel) else answer
        )
        if output_model is not None:
            data = output_model.model_validate(data).model_dump(mode="json")
        return AgentResult(
            runtime=route.primary, ok=True, text=json.dumps(data), structured=data
        )

    async def health(self) -> list[RuntimeHealth]:
        return [RuntimeHealth(name="scripted", available=True)]

    def bind_questions(self, questions: Any) -> None:
        self.questions = questions


class FakeWorkspace:
    """Implements ``Workspace`` with temporary directories and a canned diff."""

    def __init__(self, root: Path, diff_text: str = "") -> None:
        self.root = root
        self.diff_text = diff_text
        self.prepared: list[tuple[str, int | None, str]] = []
        self.released: list[Path] = []
        self.merge_conflicts: dict[str, str] = {}
        """Files a merge conflicts in, with their content (conflict markers included),
        written into the worktree by ``merge``."""
        self.merged: list[str] = []

    async def prepare(
        self, repo: str, number: int | None, sha: str, default_branch: str
    ) -> Path:
        path = self.root / f"{repo.replace('/', '__')}-{number}-{sha[:7]}"
        path.mkdir(parents=True, exist_ok=True)
        self.prepared.append((repo, number, sha))
        return path

    async def diff(
        self, path: Any, base: str, head: str, max_bytes: int = 200_000
    ) -> str:
        return self.diff_text[:max_bytes]

    async def release(self, path: Any) -> None:
        self.released.append(Path(path))

    async def merge_base(self, path: Any, a: str, b: str) -> str | None:
        return "b" * 40

    async def merge(self, path: Any, branch: str) -> MergeAttempt:
        self.merged.append(branch)
        for name, text in self.merge_conflicts.items():
            target = Path(path) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return MergeAttempt(merge_sha="d" * 40, conflicts=sorted(self.merge_conflicts))

    async def archive(self, path: Any, ref: str, max_bytes: int = 200_000_000) -> str:
        return base64.b64encode(f"code at {ref}".encode()).decode("ascii")

    async def changes(self, path: Any) -> WorkChanges:
        """The files under ``path`` that a test wrote, as a pseudo-patch."""
        p = Path(path)
        files = sorted(
            str(f.relative_to(p))
            for f in p.rglob("*")
            if f.is_file() and ".git" not in f.parts
        )
        patch = "".join(f"+++ {name}\n{(p / name).read_text()}" for name in files)
        return WorkChanges(base_sha=self.base_sha, patch=patch, files=files)

    base_sha: str = "b" * 40


class FakePublisher:
    """Implements ``BranchPublisher``; records what would have been pushed."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(
        self,
        repo: str,
        base_sha: str,
        patch: str,
        branch: str,
        message: str,
        author: tuple[str, str] | None = None,
        *,
        merge_sha: str | None = None,
        resolved: dict[str, str] | None = None,
        push_to: str | None = None,
    ) -> str:
        self.published.append(
            {
                "repo": repo,
                "base": base_sha,
                "patch": patch,
                "branch": branch,
                "message": message,
                "author": author,
                "merge_sha": merge_sha,
                "resolved": resolved or {},
                "push_to": push_to or repo,
            }
        )
        return "c" * 40


class FakeUpdater:
    """Implements ``ServiceUpdater`` with a scripted status."""

    def __init__(self, running: str = "a" * 40) -> None:
        self.running = running
        self.upstream: str | None = running
        self.pending: list[Any] = []
        self.deployed: list[str] = []
        self.fail = False

    async def status(self) -> UpdateStatus:
        return UpdateStatus(
            running_sha=self.running,
            branch="main",
            upstream_sha=self.upstream,
            pending=self.pending,
        )

    async def confirm(self) -> dict[str, Any] | None:
        return None

    async def deploy(self, sha: str) -> DeployResult:
        self.deployed.append(sha)
        if self.fail:
            return DeployResult(
                ok=False, sha=sha, previous_sha=self.running, log="tests failed"
            )
        previous, self.running = self.running, sha
        return DeployResult(ok=True, sha=sha, previous_sha=previous, log="ok")


class FakeSecrets:
    """Implements ``SecretStore`` in memory."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def describe(self, names: list[str]) -> list[SecretInfo]:
        return [
            SecretInfo(
                name=n,
                configured=n in self.values,
                source="dashboard" if n in self.values else "missing",
            )
            for n in sorted(set(names) | set(self.values))
        ]

    async def set(self, name: str, value: str, by: str) -> None:
        if not SECRET_NAME.match(name):
            raise ValueError(f"{name!r} is not a secret name")
        self.values[name] = value

    async def delete(self, name: str) -> None:
        self.values.pop(name, None)


class ActingAs:
    """A ``FakeForge`` writer acting as a person: what it posts is authored by them."""

    def __init__(self, forge: FakeForge, login: str) -> None:
        self._forge = forge
        self._login = login

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._forge, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            saved, self._forge.login = self._forge.login, self._login
            self._forge.acting_as = self._login
            try:
                return await attr(*args, **kwargs)
            finally:
                self._forge.login = saved
                self._forge.acting_as = None

        return call


class FakeUserTokens:
    """Implements ``UserTokens`` in memory."""

    def __init__(self) -> None:
        self.tokens: dict[str, UserToken] = {}

    async def get(self, login: str) -> UserToken | None:
        return self.tokens.get(login.lower())

    async def set(self, login: str, token: UserToken) -> None:
        self.tokens[login.lower()] = token

    async def delete(self, login: str) -> None:
        self.tokens.pop(login.lower(), None)


class Reconfigured:
    """Records what the service asked to run on after each settings change."""

    def __init__(self) -> None:
        self.calls: list[
            tuple[dict[str, RuntimeConfig], dict[str, ProviderBinding]]
        ] = []

    async def __call__(
        self, agents: dict[str, RuntimeConfig], providers: dict[str, ProviderBinding]
    ) -> None:
        self.calls.append((agents, providers))


class FakeLabHost:
    """Implements ``LabHost``; records commands and returns scripted results."""

    def __init__(self, name: str = "lab", gpus: list[GpuStatus] | None = None) -> None:
        self.name = name
        self.gpus = gpus or []
        self.commands: list[tuple[str, str | None, dict[str, str]]] = []
        self.results: list[CommandResult] = []
        self.stdin: list[str] = []

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        stdin: str | None = None,
        on_output: Any = None,
    ) -> CommandResult:
        self.commands.append((command, cwd, dict(env or {})))
        if stdin is not None:
            self.stdin.append(stdin)
        result = (
            self.results.pop(0)
            if self.results
            else CommandResult(exit_code=0, stdout="ok")
        )
        if on_output is not None:
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                if line:
                    await on_output(line)
        return result

    async def gpu_status(self) -> list[GpuStatus]:
        return self.gpus


class FakeLabRunner:
    """Implements the actuator's ``LabRunner``: records reproductions and answers
    them with ``outcome``; ``configure`` keeps the machines it is given."""

    def __init__(self) -> None:
        self.machines: dict[str, Any] = {}
        self.reproduced: list[dict[str, Any]] = []
        """Sessions opened, with what they were opened for."""
        self.sessions: dict[str, str] = {}
        self.seconds_left = 3600
        """What ``session_info`` says a session has left."""
        self.synced: dict[str, str] = {}
        """The checkout each session's agent may copy to the machine."""
        self.cannot_open: str | None = None
        """When set, sessions do not open, for this reason (as with no idle GPU)."""
        self.closed: list[str] = []
        self.commands: list[dict[str, Any]] = [
            {
                "command": "python check.py",
                "side": "head",
                "exit_code": 0,
                "tail": ["1 passed"],
            }
        ]
        """What a session's agent ran, as ``close_session`` reports it."""
        self.outcome: dict[str, Any] = {
            "runs": {
                "base": {
                    "exit_code": 1,
                    "timed_out": False,
                    "duration_seconds": 4.2,
                    "tail": ["AssertionError: expected 1 item, got 0"],
                },
                "head": {
                    "exit_code": 0,
                    "timed_out": False,
                    "duration_seconds": 4.0,
                    "tail": ["1 passed"],
                },
            }
        }

    def configure(self, machines: dict[str, Any]) -> None:
        self.machines = dict(machines)

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("template lab runs are not faked")

    async def open_session(
        self,
        payload: Any,
        repo: str,
        number: int,
        code: dict[str, str],
        holder: str,
        binding: Any = None,
        progress: Any = None,
        job: str | None = None,
    ) -> dict[str, Any]:
        """Open a session at once; the agent's commands are not run here."""
        if self.cannot_open:
            return {"lab": payload.lab, "deferred": True, "reason": self.cannot_open}
        if progress is not None:
            from ..domain.labs import LabProgress

            for side in getattr(payload, "sides", []) or []:
                await progress(LabProgress(side=side, phase=f"Copying {side}"))
                await progress(LabProgress(side=side, line=f"{side}: ready"))
        session_id = f"lab_{len(self.reproduced) + 1}"
        self.reproduced.append(
            {
                "payload": payload,
                "repo": repo,
                "number": number,
                "code": code,
                "holder": holder,
                "binding": binding,
                "session": session_id,
            }
        )
        self.sessions[session_id] = f"token-{session_id}"
        return {"lab": payload.lab, "session": session_id, "sides": payload.sides}

    def session_token(self, session_id: str) -> str | None:
        return self.sessions.get(session_id)

    def allow_sync(self, session_id: str, source: str) -> None:
        self.synced[session_id] = source

    def session_info(self, token: str) -> dict[str, Any] | None:
        if token not in self.sessions.values():
            return None
        return {"seconds_left": self.seconds_left}

    async def close_session(self, session_id: str) -> dict[str, Any] | None:
        if self.sessions.pop(session_id, None) is None:
            return None
        self.closed.append(session_id)
        return {"commands": list(self.commands)}

    async def check(self, lab: str) -> dict[str, Any]:
        if lab not in self.machines:
            raise RuntimeError(f"there is no test machine named {lab!r}")
        return {
            "ok": True,
            "system": "Linux 5.15 x86_64",
            "cpus": "32",
            "memory_gb": "251",
            "gpus": [
                {
                    "index": 0,
                    "memory_used_mib": 3,
                    "memory_total_mib": 81920,
                    "utilization_pct": 0,
                    "processes": 0,
                    "idle": True,
                }
            ],
        }


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, channel: str, text: str, event: str) -> None:
        self.sent.append((channel, text, event))


def make_pr(
    repo: str = "acme/widget", number: int = 42, sha: str = "a" * 40, **kwargs: Any
) -> PrSnapshot:
    """A plausible open PR for tests."""
    defaults: dict[str, Any] = {
        "title": "fix(core): handle empty batches",
        "body": "### Description\nFixes a crash.\n\n### Checklist\n- [x] tests",
        "author": "alice",
        "author_association": "CONTRIBUTOR",
        "url": f"https://example.test/{repo}/pull/{number}",
        "created_at": datetime(2026, 1, 5, 8, 0, tzinfo=utc_now().tzinfo),
        "updated_at": datetime(2026, 1, 5, 8, 0, tzinfo=utc_now().tzinfo),
    }
    defaults.update(kwargs)
    return PrSnapshot(repo=repo, number=number, head_sha=sha, **defaults)
