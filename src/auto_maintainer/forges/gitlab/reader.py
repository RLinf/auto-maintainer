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

"""``ForgeReader`` for GitLab (design 5.16).

A merge request becomes a ``PrSnapshot``: its ``iid`` is the number, its notes the
comments, its diff discussions the review threads, its approvals and reviewers'
requests for changes the reviews, and its head pipeline's jobs and the head commit's
statuses the checks. People are named by forge (``domain/forges.py``): ``alice`` on
this forge is ``corp:alice`` unless it is the default forge.

Listing open merge requests is cheap (one list); a full snapshot takes several calls,
so snapshots are kept until a merge request's light fingerprint changes or its CI is
still running, as the GitHub reader does.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from ...domain.forges import check_repo, parse_repo, person_on
from ...domain.permissions import Permission
from ...domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    IssueComment,
    IssueSnapshot,
    Mergeable,
    MergedPrSummary,
    PrFile,
    PrSnapshot,
    PrState,
    RepoInfo,
    Review,
    ReviewEventState,
    ReviewThread,
    ThreadComment,
)
from .client import GitLabClient, GitLabError, project_id

OPTIONAL = (401, 403, 404)
"""What an optional read may meet: no sign-in, no right, or nothing there."""

ROLES = {
    10: Permission.READ,
    15: Permission.READ,
    20: Permission.TRIAGE,
    30: Permission.WRITE,
    40: Permission.MAINTAIN,
    50: Permission.ADMIN,
}
"""GitLab's access levels (Guest, Planner, Reporter, Developer, Maintainer, Owner) as
the service's roles."""

_JOB_STATUS: dict[str, tuple[CheckStatus, CheckConclusion | None]] = {
    "success": (CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
    "failed": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "canceled": (CheckStatus.COMPLETED, CheckConclusion.CANCELLED),
    "canceling": (CheckStatus.IN_PROGRESS, None),
    "skipped": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
    "manual": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
    "running": (CheckStatus.IN_PROGRESS, None),
}
"""Every other job status (created, pending, preparing, scheduled, waiting) is queued."""


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _job(job: dict[str, Any]) -> CheckRun:
    status, conclusion = _JOB_STATUS.get(
        str(job.get("status")), (CheckStatus.QUEUED, None)
    )
    if conclusion == CheckConclusion.FAILURE and job.get("allow_failure"):
        conclusion = CheckConclusion.NEUTRAL
    pipeline = job.get("pipeline") or {}
    return CheckRun(
        id=int(job["id"]),
        name=str(job.get("name") or ""),
        workflow=job.get("stage"),
        status=status,
        conclusion=conclusion,
        run_id=pipeline.get("id"),
        details_url=job.get("web_url"),
        started_at=_time(job.get("started_at")),
        completed_at=_time(job.get("finished_at")),
    )


def _status(entry: dict[str, Any]) -> CheckRun:
    """A commit status set by CI outside GitLab."""
    status, conclusion = _JOB_STATUS.get(
        str(entry.get("status")), (CheckStatus.QUEUED, None)
    )
    return CheckRun(
        id=int(entry["id"]),
        name=str(entry.get("name") or ""),
        status=status,
        conclusion=conclusion,
        details_url=entry.get("target_url"),
        started_at=_time(entry.get("started_at")),
        completed_at=_time(entry.get("finished_at")),
        summary=entry.get("description"),
    )


def _diff_counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _change_type(d: dict[str, Any]) -> str:
    if d.get("new_file"):
        return "added"
    if d.get("deleted_file"):
        return "removed"
    if d.get("renamed_file"):
        return "renamed"
    return "modified"


def _fingerprint(mr: dict[str, Any]) -> str:
    parts = [
        str(mr.get(k))
        for k in ("updated_at", "sha", "user_notes_count", "draft", "merge_status")
    ]
    parts.append(",".join(sorted(mr.get("labels") or [])))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class GitLabReader:
    """Implements ``ForgeReader`` for one GitLab forge.

    Args:
        client: Its API client.
        forge: The forge's id, for people's names.
        snapshot_cache_seconds: How long a full snapshot is kept without changes.
        clock: For tests.
    """

    def __init__(
        self,
        client: GitLabClient,
        forge: str,
        *,
        snapshot_cache_seconds: float = 900,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self.forge = forge
        self._cache_seconds = snapshot_cache_seconds
        self._snapshots: dict[str, dict[int, tuple[float, str, PrSnapshot]]] = {}
        self._defaults: dict[str, str] = {}
        self._user_ids: dict[str, int | None] = {}
        self._clock = clock or (lambda: datetime.now(UTC).timestamp())
        self._limit = asyncio.Semaphore(2 if client.anonymous else 8)

    def _person(self, user: dict[str, Any] | None) -> str:
        return person_on(self.forge, str((user or {}).get("username") or "ghost"))

    async def service_login(self) -> str:
        user = await self._client.get("/user")
        return str(user["username"])

    async def _default_branch(self, repo: str) -> str:
        if repo not in self._defaults:
            info = await self.get_repo(repo)
            self._defaults[repo] = info.default_branch
        return self._defaults[repo]

    async def get_repo(self, repo: str) -> RepoInfo:
        pid = project_id(repo)
        p = await self._client.get(f"/projects/{pid}")
        labels = await self._client.paginate(
            f"/projects/{pid}/labels", limit=500, allow=OPTIONAL
        )
        branch = str(p.get("default_branch") or "main")
        self._defaults[repo] = branch
        return RepoInfo(
            name=check_repo(repo),
            default_branch=branch,
            private=p.get("visibility") != "public",
            labels=[str(label["name"]) for label in labels],
            required_approvals=None,
        )

    async def list_open_prs(self, repo: str) -> list[PrSnapshot]:
        light = await self._client.paginate(
            f"/projects/{project_id(repo)}/merge_requests",
            {"state": "opened", "order_by": "updated_at"},
            limit=1000,
        )
        cache = self._snapshots.setdefault(repo, {})
        now = self._clock()
        stale = []
        for mr in light:
            number = int(mr["iid"])
            kept = cache.get(number)
            running = kept is not None and any(
                c.status != CheckStatus.COMPLETED for c in kept[2].checks
            )
            if (
                kept is None
                or kept[1] != _fingerprint(mr)
                or running
                or now - kept[0] > self._cache_seconds
            ):
                stale.append(mr)
        fresh = await asyncio.gather(*(self._full(repo, mr) for mr in stale))
        for mr, snap in zip(stale, fresh, strict=True):
            cache[int(mr["iid"])] = (now, _fingerprint(mr), snap)
        open_numbers = {int(mr["iid"]) for mr in light}
        for number in list(cache):
            if number not in open_numbers:
                del cache[number]
        return [cache[int(mr["iid"])][2] for mr in light]

    async def get_pr(self, repo: str, number: int) -> PrSnapshot | None:
        try:
            mr = await self._client.get(
                f"/projects/{project_id(repo)}/merge_requests/{number}",
                params={"include_diverged_commits_count": "true"},
            )
        except GitLabError as e:
            if e.status == 404:
                return None
            raise
        snap = await self._full(repo, mr, detailed=True)
        if snap.state == PrState.OPEN:
            self._snapshots.setdefault(repo, {})[number] = (
                self._clock(),
                _fingerprint(mr),
                snap,
            )
        return snap

    async def _full(
        self, repo: str, mr: dict[str, Any], detailed: bool = False
    ) -> PrSnapshot:
        async with self._limit:
            pid = project_id(repo)
            number = int(mr["iid"])
            base = f"/projects/{pid}/merge_requests/{number}"
            # Notes are never optional for the service: missing ones would hide its
            # own reviews. Only an anonymous read-only client goes on without them.
            hidden = (401,) if self._client.anonymous else ()
            if not detailed:
                mr = await self._client.get(
                    base, params={"include_diverged_commits_count": "true"}
                )
            (
                diffs,
                commits,
                notes,
                discussions,
                approvals,
                reviewers,
            ) = await asyncio.gather(
                self._client.paginate(f"{base}/diffs", limit=3000),
                self._client.paginate(f"{base}/commits", limit=1000),
                self._client.paginate(
                    f"{base}/notes",
                    {"sort": "asc", "order_by": "created_at"},
                    allow=hidden,
                ),
                self._client.paginate(f"{base}/discussions", allow=hidden),
                self._client.get(f"{base}/approvals", allow=OPTIONAL),
                self._client.get(f"{base}/reviewers", allow=OPTIONAL),
            )
            checks = await self._checks(pid, mr)
            association = await self._association(repo, mr.get("author"))
        head = str(mr.get("sha") or "")
        files, added, removed = [], 0, 0
        for d in diffs:
            a, r = _diff_counts(str(d.get("diff") or ""))
            added, removed = added + a, removed + r
            files.append(
                PrFile(
                    path=str(d.get("new_path") or d.get("old_path")),
                    additions=a,
                    deletions=r,
                    change_type=_change_type(d),
                )
            )
        reviews = [
            Review(
                id=int((a.get("user") or {}).get("id") or 0),
                author=self._person(a.get("user")),
                state=ReviewEventState.APPROVED,
                submitted_at=_time(a.get("approved_at")),
                commit_sha=head,
            )
            for a in (approvals or {}).get("approved_by") or []
        ]
        reviews += [
            Review(
                id=int((r.get("user") or {}).get("id") or 0),
                author=self._person(r.get("user")),
                state=ReviewEventState.CHANGES_REQUESTED,
                commit_sha=head,
            )
            for r in reviewers or []
            if r.get("state") == "requested_changes"
        ]
        threads = []
        for d in discussions:
            dnotes = [n for n in d.get("notes") or [] if not n.get("system")]
            first = dnotes[0] if dnotes else {}
            position = first.get("position") or {}
            if not dnotes or not position:
                continue
            threads.append(
                ReviewThread(
                    id=str(d["id"]),
                    is_resolved=all(
                        n.get("resolved") for n in dnotes if n.get("resolvable")
                    ),
                    is_outdated=bool(head) and position.get("head_sha") != head,
                    path=position.get("new_path") or position.get("old_path"),
                    line=position.get("new_line") or position.get("old_line"),
                    comments=[
                        ThreadComment(
                            id=int(n["id"]),
                            author=self._person(n.get("author")),
                            body=str(n.get("body") or ""),
                            commit_sha=(n.get("position") or {}).get("head_sha"),
                            created_at=_time(n.get("created_at")),
                        )
                        for n in dnotes
                    ],
                )
            )
        in_threads = {c.id for t in threads for c in t.comments}
        comments = [
            IssueComment(
                id=int(n["id"]),
                author=self._person(n.get("author")),
                body=str(n.get("body") or ""),
                created_at=_time(n.get("created_at")),
                updated_at=_time(n.get("updated_at")),
            )
            for n in notes
            if not n.get("system") and int(n["id"]) not in in_threads
        ]
        state = {"merged": PrState.MERGED, "opened": PrState.OPEN}.get(
            str(mr.get("state")), PrState.CLOSED
        )
        detailed_status = str(mr.get("detailed_merge_status") or mr.get("merge_status"))
        if mr.get("has_conflicts"):
            mergeable = Mergeable.CONFLICTING
        elif detailed_status in (
            "checking",
            "unchecked",
            "preparing",
            "cannot_be_merged_recheck",
        ):
            mergeable = Mergeable.UNKNOWN
        else:
            mergeable = Mergeable.MERGEABLE
        diverged = mr.get("diverged_commits_count")
        same = mr.get("source_project_id") == mr.get("target_project_id")
        return PrSnapshot(
            repo=check_repo(repo),
            number=number,
            title=str(mr.get("title") or ""),
            body=str(mr.get("description") or ""),
            author=self._person(mr.get("author")),
            author_association=association,
            is_draft=bool(mr.get("draft") or mr.get("work_in_progress")),
            state=state,
            base_ref=str(mr.get("target_branch") or ""),
            head_ref=str(mr.get("source_branch") or ""),
            head_sha=head,
            head_repo=check_repo(repo) if same else await self._fork_of(mr),
            url=str(mr.get("web_url") or ""),
            created_at=_time(mr.get("created_at")),
            updated_at=_time(mr.get("updated_at")),
            labels=[str(x) for x in mr.get("labels") or []],
            files=files,
            commits=[
                Commit(
                    sha=str(c["id"]),
                    message=str(c.get("message") or ""),
                    author_email=c.get("author_email"),
                    committed_at=_time(c.get("committed_date")),
                )
                for c in commits
            ],
            reviews=reviews,
            comments=comments,
            threads=threads,
            checks=checks,
            mergeable=mergeable,
            behind_base=(diverged > 0) if isinstance(diverged, int) else None,
            additions=added,
            deletions=removed,
            requested_reviewers=[self._person(r) for r in mr.get("reviewers") or []],
            assignees=[self._person(a) for a in mr.get("assignees") or []],
        )

    async def _checks(self, pid: str, mr: dict[str, Any]) -> list[CheckRun]:
        checks: list[CheckRun] = []
        pipeline = mr.get("head_pipeline") or {}
        if pipeline.get("id"):
            # A fork's merge request may run its pipeline in the fork's project.
            where = pipeline.get("project_id") or pid
            jobs = await self._client.paginate(
                f"/projects/{where}/pipelines/{pipeline['id']}/jobs", allow=OPTIONAL
            )
            checks += [_job(j) for j in jobs]
        sha = mr.get("sha")
        if sha:
            statuses = await self._client.get(
                f"/projects/{pid}/repository/commits/{sha}/statuses",
                params={"all": "false"},
                allow=OPTIONAL,
            )
            checks += [
                _status(s)
                for s in statuses or []
                if s.get("name") not in {c.name for c in checks}
            ]
        return checks

    async def _fork_of(self, mr: dict[str, Any]) -> str | None:
        source = mr.get("source_project_id")
        if not source:
            return None
        p = await self._client.get(f"/projects/{source}", allow=OPTIONAL)
        if not p:
            return None
        return check_repo(f"{self.forge}:{p['path_with_namespace']}")

    async def _association(self, repo: str, user: dict[str, Any] | None) -> str:
        """``MEMBER`` for a Developer or above on the project, else ``CONTRIBUTOR``,
        in the words the review rules use."""
        user_id = (user or {}).get("id")
        if not user_id:
            return "NONE"
        member = await self._client.get(
            f"/projects/{project_id(repo)}/members/all/{user_id}", allow=OPTIONAL
        )
        level = int((member or {}).get("access_level") or 0)
        return "MEMBER" if level >= 30 else "CONTRIBUTOR"

    async def list_open_issues(self, repo: str) -> list[IssueSnapshot]:
        issues = await self._client.paginate(
            f"/projects/{project_id(repo)}/issues", {"state": "opened"}, limit=500
        )
        return [
            IssueSnapshot(
                repo=check_repo(repo),
                number=int(i["iid"]),
                title=str(i.get("title") or ""),
                body=str(i.get("description") or ""),
                author=self._person(i.get("author")),
                labels=[str(x) for x in i.get("labels") or []],
                created_at=_time(i.get("created_at")),
                url=str(i.get("web_url") or ""),
            )
            for i in issues
        ]

    async def list_merged_prs(
        self, repo: str, limit: int = 50
    ) -> list[MergedPrSummary]:
        mrs = await self._client.get(
            f"/projects/{project_id(repo)}/merge_requests",
            params={
                "state": "merged",
                "order_by": "updated_at",
                "sort": "desc",
                "per_page": max(1, min(limit, 100)),
            },
        )
        return [
            MergedPrSummary(
                number=int(m["iid"]),
                title=str(m.get("title") or ""),
                author=self._person(m.get("author")),
                labels=[str(x) for x in m.get("labels") or []],
                merged_at=_time(m.get("merged_at")),
                review_count=int(m.get("user_notes_count") or 0),
            )
            for m in mrs or []
        ]

    async def get_file(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        ref = ref or await self._default_branch(repo)
        return await self._client.get(
            f"/projects/{project_id(repo)}/repository/files/"
            f"{quote(path.lstrip('/'), safe='')}/raw",
            params={"ref": ref},
            allow=(404,),
            raw=True,
        )

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        ref = ref or await self._default_branch(repo)
        try:
            entries = await self._client.paginate(
                f"/projects/{project_id(repo)}/repository/tree",
                {"path": path.strip("/"), "ref": ref},
                limit=1000,
            )
        except GitLabError:
            return []
        return [str(e["name"]) for e in entries if e.get("name")]

    async def get_pr_diff(self, repo: str, number: int) -> str | None:
        """A unified diff of the merge request, in git's format."""
        try:
            diffs = await self._client.paginate(
                f"/projects/{project_id(repo)}/merge_requests/{number}/diffs",
                limit=3000,
            )
        except GitLabError:
            return None
        out = []
        for d in diffs:
            old = d.get("old_path") or d.get("new_path")
            new = d.get("new_path") or old
            out.append(f"diff --git a/{old} b/{new}")
            out.append("--- /dev/null" if d.get("new_file") else f"--- a/{old}")
            out.append("+++ /dev/null" if d.get("deleted_file") else f"+++ b/{new}")
            body = str(d.get("diff") or "")
            out.append(body if body.endswith("\n") else body + "\n")
        return "\n".join(out)

    async def _user_id(self, login: str) -> int | None:
        if login not in self._user_ids:
            users = await self._client.get("/users", params={"username": login})
            self._user_ids[login] = int(users[0]["id"]) if users else None
        return self._user_ids[login]

    async def get_permission(self, repo: str, login: str) -> Permission:
        user_id = await self._user_id(login)
        if user_id is None:
            return Permission.NONE
        member = await self._client.get(
            f"/projects/{project_id(repo)}/members/all/{user_id}", allow=OPTIONAL
        )
        if member:
            return ROLES.get(int(member.get("access_level") or 0), Permission.NONE)
        info = await self._client.get(f"/projects/{project_id(repo)}", allow=OPTIONAL)
        return (
            Permission.READ
            if info and info.get("visibility") == "public"
            else Permission.NONE
        )

    async def is_team_member(self, repo: str, team: str, login: str) -> bool:
        """Whether ``login`` belongs to the group ``team`` (a group path)."""
        user_id = await self._user_id(login)
        if user_id is None:
            return False
        member = await self._client.get(
            f"/groups/{quote(team, safe='')}/members/all/{user_id}", allow=OPTIONAL
        )
        return bool(member)

    async def path_changed_by(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Who merged the latest change to ``path``: the merge request's merger, else
        nobody known (a commit's author is an email address, not a login)."""
        pid = project_id(repo)
        commits = await self._client.get(
            f"/projects/{pid}/repository/commits",
            params={
                "path": path,
                "ref_name": ref or await self._default_branch(repo),
                "per_page": 1,
            },
            allow=(404,),
        )
        if not commits:
            return None
        mrs = await self._client.get(
            f"/projects/{pid}/repository/commits/{commits[0]['id']}/merge_requests",
            allow=(404,),
        )
        merged_by = next(
            (m.get("merged_by") for m in mrs or [] if m.get("merged_by")), None
        )
        return self._person(merged_by) if merged_by else None

    async def default_branch_sha(self, repo: str) -> str:
        branch = await self._default_branch(repo)
        data = await self._client.get(
            f"/projects/{project_id(repo)}/repository/branches/{quote(branch, safe='')}"
        )
        return str(data["commit"]["id"])


def forge_path(repo: str) -> str:
    """``repo``'s path on its forge."""
    return parse_repo(repo).path
