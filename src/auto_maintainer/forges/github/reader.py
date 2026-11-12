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

"""``ForgeReader`` for GitHub.

``list_open_prs`` works in two passes to stay inside GraphQL rate limits. A light
query lists every open PR with its ``updatedAt``, head SHA, and the ``updatedAt`` of
the head commit's check suites (check results do not change a PR's ``updatedAt``).
Full snapshots are fetched, ten PRs per query, only for PRs whose light fingerprint
changed or whose cached snapshot is older than ``snapshot_cache_seconds``.

Nested connections are fetched to these limits and truncated beyond them:

==================  ==========================================
connection          limit
==================  ==========================================
labels              first 50
files               first 100
commits             last 100
reviews             last 50
issue comments      last 100
review threads      last 50, with the first 20 comments each
review requests     first 20
check suites        first 20 on the head commit
check runs          first 50 per suite, latest attempt only
==================  ==========================================

``behind_base`` comes from ``mergeStateStatus``: ``BEHIND`` is ``True``; ``CLEAN``,
``UNSTABLE``, and ``HAS_HOOKS`` are ``False``; any other state is ``None``. GitHub
reports ``BEHIND`` only when the base branch requires branches to be up to date.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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
from .client import GitHubClient, GitHubError, GraphQLError

PR_BATCH = 10
LIGHT_PAGE = 50

_FILE = "path additions deletions changeType"
_REVIEW = "databaseId author { login } state body submittedAt commit { oid }"
_COMMENT = "databaseId author { login } body createdAt updatedAt"
_THREAD = (
    "id isResolved isOutdated path line "
    "comments(first: 20) { nodes { databaseId author { login } body createdAt commit { oid } } }"
)
_RUN = "databaseId name status conclusion detailsUrl startedAt completedAt summary"
_FORWARD = "pageInfo { hasNextPage endCursor }"
_BACKWARD = "pageInfo { hasPreviousPage startCursor }"

_PR_FIELDS = (
    """
fragment PrFields on PullRequest {
  id number title body url createdAt updatedAt isDraft state authorAssociation
  author { login }
  baseRefName headRefName headRefOid
  headRepository { nameWithOwner }
  maintainerCanModify reviewDecision
  mergeable mergeStateStatus additions deletions
  labels(first: 50) { nodes { name } }
  files(first: 100) { FORWARD nodes { FILE } }
  commits(last: 100) { nodes { commit { oid message committedDate author { email user { login } } } } }
  reviews(last: 50) { BACKWARD nodes { REVIEW } }
  comments(last: 100) { BACKWARD nodes { COMMENT } }
  reviewThreads(last: 50) { BACKWARD nodes { THREAD } }
  reviewRequests(first: 20) {
    nodes { asCodeOwner requestedReviewer { ... on User { login } ... on Team { slug } ... on Bot { login } } }
  }
  assignees(first: 20) { nodes { login } }
  headCommit: commits(last: 1) {
    nodes {
      commit {
        checkSuites(first: 50) {
          nodes {
            id status conclusion
            workflowRun { databaseId runAttempt url workflow { name } }
            checkRuns(first: 50, filterBy: {checkType: LATEST}) { FORWARD nodes { RUN } }
          }
        }
      }
    }
  }
}
""".replace("FORWARD", _FORWARD)
    .replace("BACKWARD", _BACKWARD)
    .replace("FILE", _FILE)
    .replace("REVIEW", _REVIEW)
    .replace("COMMENT", _COMMENT)
    .replace("THREAD", _THREAD)
    .replace("RUN", _RUN)
)

_MORE_PAGES = 20
"""At most this many further pages of one list of a PR (100 items each)."""

_MORE: dict[str, tuple[str, str, bool]] = {
    "files": ("files(first: 100, after: $cursor)", _FILE, True),
    "reviews": ("reviews(last: 100, before: $cursor)", _REVIEW, False),
    "comments": ("comments(last: 100, before: $cursor)", _COMMENT, False),
    "reviewThreads": ("reviewThreads(last: 100, before: $cursor)", _THREAD, False),
}
"""The PR's lists one query may cut short: how to read more of each, and whether
it reads forward (from the first) or backward (from the latest)."""

_LIGHT_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: PAGE_SIZE, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number updatedAt headRefOid
        commits(last: 1) { nodes { commit { checkSuites(first: 20) { totalCount nodes { updatedAt status } } } } }
      }
    }
  }
}
""".replace("PAGE_SIZE", str(LIGHT_PAGE))

_MERGED_QUERY = """
query($owner: String!, $name: String!, $count: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, last: $count, orderBy: {field: UPDATED_AT, direction: ASC}) {
      nodes {
        number title mergedAt
        author { login }
        labels(first: 20) { nodes { name } }
        reviews { totalCount }
        commits(last: 20) { nodes { commit { message } } }
      }
    }
  }
}
"""

_CHECK_STATUS = {
    "QUEUED": CheckStatus.QUEUED,
    "IN_PROGRESS": CheckStatus.IN_PROGRESS,
    "COMPLETED": CheckStatus.COMPLETED,
    "WAITING": CheckStatus.QUEUED,
    "PENDING": CheckStatus.QUEUED,
    "REQUESTED": CheckStatus.QUEUED,
}

_BEHIND = {"BEHIND": True, "CLEAN": False, "UNSTABLE": False, "HAS_HOOKS": False}


_MERGE_SETTINGS = (
    ("squash", "allow_squash_merge"),
    ("merge", "allow_merge_commit"),
    ("rebase", "allow_rebase_merge"),
)


def _merge_methods(data: dict[str, Any]) -> list[str] | None:
    """The merge methods a repository allows; GitHub shows its settings only to
    accounts that may push, so ``None`` for the others."""
    if not any(key in data for _, key in _MERGE_SETTINGS):
        return None
    return [method for method, key in _MERGE_SETTINGS if data.get(key)]


def map_check_status(value: str | None) -> CheckStatus:
    """Map a GitHub check status (any case) to the domain enum."""
    return _CHECK_STATUS.get((value or "").upper(), CheckStatus.QUEUED)


def map_check_conclusion(value: str | None) -> CheckConclusion | None:
    """Map a GitHub check conclusion (any case) to the domain enum, or ``None``."""
    if not value:
        return None
    try:
        return CheckConclusion(value.lower())
    except ValueError:
        return None


def _login(node: dict[str, Any] | None) -> str:
    return (node or {}).get("login") or "ghost"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _nodes(conn: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [n for n in (conn or {}).get("nodes") or [] if n]


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def snapshot_from_graphql(repo: str, node: dict[str, Any]) -> PrSnapshot:
    """Build a ``PrSnapshot`` from a ``PrFields`` GraphQL node."""
    head = _nodes(node.get("headCommit"))
    suites = _nodes(head[0]["commit"].get("checkSuites")) if head else []
    checks: list[CheckRun] = []
    for suite in suites:
        run = suite.get("workflowRun") or {}
        workflow = ((run.get("workflow") or {}).get("name")) if run else None
        runs_in_suite = _nodes(suite.get("checkRuns"))
        if (
            run
            and not runs_in_suite
            and (suite.get("conclusion") or "").upper() == "ACTION_REQUIRED"
        ):
            # A workflow GitHub holds until a maintainer approves it (a first-time
            # contributor's fork PR): it has no jobs yet, and it has not run.
            checks.append(
                CheckRun(
                    id=int(run["databaseId"]),
                    name=workflow or "workflow",
                    workflow=workflow,
                    status=CheckStatus.QUEUED,
                    conclusion=CheckConclusion.ACTION_REQUIRED,
                    run_id=run.get("databaseId"),
                    run_attempt=run.get("runAttempt"),
                    details_url=run.get("url"),
                    summary="waiting for a maintainer to approve running it",
                )
            )
            continue
        for cr in runs_in_suite:
            checks.append(
                CheckRun(
                    id=int(cr["databaseId"]),
                    name=cr.get("name") or "",
                    workflow=workflow,
                    status=map_check_status(cr.get("status")),
                    conclusion=map_check_conclusion(cr.get("conclusion")),
                    run_id=run.get("databaseId") if run else None,
                    run_attempt=run.get("runAttempt") if run else None,
                    details_url=cr.get("detailsUrl"),
                    started_at=_dt(cr.get("startedAt")),
                    completed_at=_dt(cr.get("completedAt")),
                    summary=cr.get("summary"),
                )
            )
    threads = [
        ReviewThread(
            id=t["id"],
            is_resolved=bool(t.get("isResolved")),
            is_outdated=bool(t.get("isOutdated")),
            path=t.get("path"),
            line=t.get("line"),
            comments=[
                ThreadComment(
                    id=int(c["databaseId"]),
                    author=_login(c.get("author")),
                    body=c.get("body") or "",
                    commit_sha=(c.get("commit") or {}).get("oid"),
                    created_at=_dt(c.get("createdAt")),
                )
                for c in _nodes(t.get("comments"))
            ],
        )
        for t in _nodes(node.get("reviewThreads"))
    ]
    requested, teams, direct = [], [], []
    for r in _nodes(node.get("reviewRequests")):
        reviewer = r.get("requestedReviewer") or {}
        if reviewer.get("login"):
            requested.append(reviewer["login"])
            if not r.get("asCodeOwner"):
                direct.append(reviewer["login"])
        elif reviewer.get("slug"):
            teams.append(reviewer["slug"])
    commits = []
    for c in _nodes(node.get("commits")):
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        commits.append(
            Commit(
                sha=commit["oid"],
                message=commit.get("message") or "",
                author_login=(author.get("user") or {}).get("login"),
                author_email=author.get("email"),
                committed_at=_dt(commit.get("committedDate")),
            )
        )
    return PrSnapshot(
        repo=repo,
        number=int(node["number"]),
        title=node.get("title") or "",
        body=node.get("body") or "",
        author=_login(node.get("author")),
        author_association=node.get("authorAssociation") or "NONE",
        is_draft=bool(node.get("isDraft")),
        state=PrState(node.get("state") or "OPEN"),
        base_ref=node.get("baseRefName") or "",
        head_ref=node.get("headRefName") or "",
        head_sha=node["headRefOid"],
        head_repo=(node.get("headRepository") or {}).get("nameWithOwner"),
        maintainer_can_modify=node.get("maintainerCanModify"),
        review_decision=node.get("reviewDecision"),
        url=node.get("url") or "",
        created_at=_dt(node.get("createdAt")),
        updated_at=_dt(node.get("updatedAt")),
        labels=[n["name"] for n in _nodes(node.get("labels"))],
        files=[
            PrFile(
                path=f["path"],
                additions=f.get("additions") or 0,
                deletions=f.get("deletions") or 0,
                change_type=f.get("changeType") or "MODIFIED",
            )
            for f in _nodes(node.get("files"))
        ],
        commits=commits,
        reviews=[
            Review(
                id=int(r["databaseId"]),
                author=_login(r.get("author")),
                state=ReviewEventState(r.get("state") or "COMMENTED"),
                body=r.get("body") or "",
                submitted_at=_dt(r.get("submittedAt")),
                commit_sha=(r.get("commit") or {}).get("oid"),
            )
            for r in _nodes(node.get("reviews"))
        ],
        comments=[
            IssueComment(
                id=int(c["databaseId"]),
                author=_login(c.get("author")),
                body=c.get("body") or "",
                created_at=_dt(c.get("createdAt")),
                updated_at=_dt(c.get("updatedAt")),
            )
            for c in _nodes(node.get("comments"))
        ],
        threads=threads,
        checks=checks,
        mergeable=Mergeable(node.get("mergeable") or "UNKNOWN"),
        behind_base=_BEHIND.get(node.get("mergeStateStatus") or ""),
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        requested_reviewers=requested,
        requested_directly=direct,
        requested_teams=teams,
        assignees=[a["login"] for a in _nodes(node.get("assignees")) if a.get("login")],
    )


def _light_fingerprint(node: dict[str, Any]) -> str:
    head = _nodes(node.get("commits"))
    suites = (head[0]["commit"].get("checkSuites") or {}) if head else {}
    stamps = sorted(f"{s.get('updatedAt')}:{s.get('status')}" for s in _nodes(suites))
    return "|".join(
        [
            node.get("updatedAt") or "",
            node.get("headRefOid") or "",
            str(suites.get("totalCount", 0)),
            *stamps,
        ]
    )


class GitHubReader:
    """``ForgeReader`` backed by the GitHub REST and GraphQL APIs.

    Args:
        client: The API client.
        snapshot_cache_seconds: Refetch a PR's full snapshot at least this often even
            when its light fingerprint is unchanged.
        clock: Returns the current Unix time; for tests.
    """

    def __init__(
        self,
        client: GitHubClient,
        *,
        snapshot_cache_seconds: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cache_seconds = snapshot_cache_seconds
        self._clock = clock
        self._snapshots: dict[str, dict[int, tuple[str, float, PrSnapshot]]] = {}
        self._login: str | None = None

    def invalidate(self, repo: str, number: int | None = None) -> None:
        """Forget cached snapshots for a PR, or for every PR of ``repo``."""
        if number is None:
            self._snapshots.pop(repo, None)
        else:
            self._snapshots.get(repo, {}).pop(number, None)

    async def service_login(self) -> str:
        if self._login:
            return self._login
        data = await self._client.get("/user")
        self._login = data["login"]
        return self._login

    # ------------------------------------------------------------------ repository

    async def get_repo(self, repo: str) -> RepoInfo:
        data = await self._client.get(f"/repos/{repo}", repo=repo)
        branch = data.get("default_branch") or "main"
        branch_data = (
            await self._client.get(
                f"/repos/{repo}/branches/{quote(branch, safe='')}",
                repo=repo,
                allow_404=True,
            )
            or {}
        )
        labels = [
            label["name"]
            for label in await self._client.paginate(f"/repos/{repo}/labels", repo=repo)
        ]

        required: set[str] | None = None
        strict: bool | None = None
        approvals: int | None = None
        owners_review: bool | None = None
        last_push: bool | None = None
        stale: bool | None = None
        protection_summary = (branch_data.get("protection") or {}).get(
            "required_status_checks"
        ) or {}
        if branch_data:
            required = set(protection_summary.get("contexts") or [])
            required |= {
                c.get("context")
                for c in protection_summary.get("checks") or []
                if c.get("context")
            }
            if not branch_data.get("protected"):
                strict, approvals, owners_review = False, 0, False
                last_push, stale = False, False
        protection = None
        if branch_data.get("protected"):
            protection = await self._client.get(
                f"/repos/{repo}/branches/{quote(branch, safe='')}/protection",
                repo=repo,
                allow=(403, 404),
            )
        if protection:
            checks = protection.get("required_status_checks") or {}
            required = (required or set()) | set(checks.get("contexts") or [])
            strict = bool(checks.get("strict")) if checks else False
            reviews = protection.get("required_pull_request_reviews") or {}
            approvals = int(reviews.get("required_approving_review_count") or 0)
            owners_review = bool(reviews.get("require_code_owner_reviews"))
            last_push = bool(reviews.get("require_last_push_approval"))
            stale = bool(reviews.get("dismiss_stale_reviews"))
        rules = await self._client.get(
            f"/repos/{repo}/rules/branches/{quote(branch, safe='')}",
            repo=repo,
            allow=(403, 404),
        )
        for rule in rules or []:
            params = rule.get("parameters") or {}
            if rule.get("type") == "required_status_checks":
                required = (required or set()) | {
                    c["context"]
                    for c in params.get("required_status_checks") or []
                    if c.get("context")
                }
                if params.get("strict_required_status_checks_policy"):
                    strict = True
                elif strict is None:
                    strict = False
            elif rule.get("type") == "pull_request":
                count = int(params.get("required_approving_review_count") or 0)
                approvals = max(approvals or 0, count)
                owners_review = bool(
                    owners_review or params.get("require_code_owner_review")
                )
                last_push = bool(last_push or params.get("require_last_push_approval"))
                stale = bool(stale or params.get("dismiss_stale_reviews_on_push"))
        return RepoInfo(
            # GitHub's own spelling: names are not case-sensitive there, and the
            # service keys a repository's records and memory folder by this one.
            name=data.get("full_name") or repo,
            default_branch=branch,
            default_branch_sha=((branch_data.get("commit") or {}).get("sha")),
            private=bool(data.get("private")),
            labels=labels,
            required_checks=sorted(required) if required is not None else None,
            requires_up_to_date=strict,
            required_approvals=approvals,
            requires_code_owner_reviews=owners_review,
            requires_last_push_approval=last_push,
            dismisses_stale_reviews=stale,
            merge_methods=_merge_methods(data),
        )

    async def behind_by(self, repo: str, base: str, head: str) -> int | None:
        data = await self._client.get(
            f"/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
            repo=repo,
            params={"per_page": 1},
            allow=(403, 404, 422),
        )
        behind = (data or {}).get("behind_by")
        return int(behind) if isinstance(behind, int) else None

    async def default_branch_sha(self, repo: str) -> str:
        data = await self._client.get(f"/repos/{repo}", repo=repo)
        branch = data.get("default_branch") or "main"
        ref = await self._client.get(
            f"/repos/{repo}/git/ref/heads/{quote(branch, safe='')}", repo=repo
        )
        return ref["object"]["sha"]

    # ------------------------------------------------------------------ pull requests

    async def list_open_prs(self, repo: str) -> list[PrSnapshot]:
        owner, name = _split(repo)
        light: list[dict[str, Any]] = []
        cursor = None
        while True:
            data = await self._client.graphql(
                _LIGHT_QUERY,
                {"owner": owner, "name": name, "cursor": cursor},
                repo=repo,
            )
            conn = data["repository"]["pullRequests"]
            light.extend(_nodes(conn))
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]

        cache = self._snapshots.setdefault(repo, {})
        now = self._clock()
        stale: list[int] = []
        fingerprints: dict[int, str] = {}
        for node in light:
            number = int(node["number"])
            fp = _light_fingerprint(node)
            fingerprints[number] = fp
            cached = cache.get(number)
            if (
                cached is None
                or cached[0] != fp
                or now - cached[1] >= self._cache_seconds
            ):
                stale.append(number)
        fetched = await self._fetch_prs(repo, stale)
        for number in stale:
            if number in fetched:
                cache[number] = (fingerprints[number], now, fetched[number])
        for number in list(cache):
            if number not in fingerprints:
                del cache[number]
        return [cache[n][2] for n in sorted(fingerprints) if n in cache]

    async def get_pr(self, repo: str, number: int) -> PrSnapshot | None:
        return (await self._fetch_prs(repo, [number])).get(number)

    async def _fetch_prs(self, repo: str, numbers: list[int]) -> dict[int, PrSnapshot]:
        owner, name = _split(repo)
        found: dict[int, PrSnapshot] = {}
        for start in range(0, len(numbers), PR_BATCH):
            batch = numbers[start : start + PR_BATCH]
            fields = "\n".join(
                f"p{n}: pullRequest(number: {n}) {{ ...PrFields }}" for n in batch
            )
            query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}\n{_PR_FIELDS}"
            try:
                data = await self._client.graphql(
                    query,
                    {"owner": owner, "name": name},
                    repo=repo,
                    allow_not_found=True,
                )
            except GraphQLError as err:
                if not err.not_found_only:
                    raise
                data = err.data or {}
            repository = data.get("repository") or {}
            for n in batch:
                node = repository.get(f"p{n}")
                if node:
                    await self._complete(repo, node)
                    found[n] = snapshot_from_graphql(repo, node)
        return found

    async def _complete(self, repo: str, node: dict[str, Any]) -> None:
        """Read, in place, the rest of every list of the PR ``node`` that one query
        cut short: its files, reviews, comments, and review threads, and each check
        suite's runs (a matrix workflow has more than one page). A PR is judged on
        all of them: a failed job past the first page still fails its CI."""
        pr_id = node.get("id")
        for field, (call, fields, forward) in _MORE.items():
            conn = node.get(field)
            if not pr_id or not isinstance(conn, dict):
                continue
            await self._more(
                repo,
                conn,
                pr_id,
                f"... on PullRequest {{ {field}: {call} {{ PAGE nodes {{ {fields} }} }} }}",
                field,
                forward,
            )
        for head in _nodes(node.get("headCommit")):
            for suite in _nodes((head.get("commit") or {}).get("checkSuites")):
                conn = suite.get("checkRuns")
                if not suite.get("id") or not isinstance(conn, dict):
                    continue
                await self._more(
                    repo,
                    conn,
                    suite["id"],
                    "... on CheckSuite { checkRuns(first: 100, after: $cursor, "
                    f"filterBy: {{checkType: LATEST}}) {{ PAGE nodes {{ {_RUN} }} }} }}",
                    "checkRuns",
                    True,
                )

    async def _more(
        self,
        repo: str,
        conn: dict[str, Any],
        node_id: str,
        selection: str,
        field: str,
        forward: bool,
    ) -> None:
        """Add the further pages of the connection ``conn`` to its nodes."""
        info = conn.get("pageInfo") or {}
        for _ in range(_MORE_PAGES):
            more = info.get("hasNextPage" if forward else "hasPreviousPage")
            cursor = info.get("endCursor" if forward else "startCursor")
            if not more or not cursor:
                return
            query = (
                "query($id: ID!, $cursor: String) { node(id: $id) { "
                + selection.replace("PAGE", _FORWARD if forward else _BACKWARD)
                + " } }"
            )
            data = await self._client.graphql(
                query, {"id": node_id, "cursor": cursor}, repo=repo
            )
            page = (data.get("node") or {}).get(field) or {}
            got = [n for n in page.get("nodes") or [] if n]
            have = [n for n in conn.get("nodes") or [] if n]
            conn["nodes"] = have + got if forward else got + have
            info = page.get("pageInfo") or {}

    async def list_open_issues(self, repo: str) -> list[IssueSnapshot]:
        items = await self._client.paginate(
            f"/repos/{repo}/issues", repo=repo, params={"state": "open"}, max_pages=5
        )
        return [
            IssueSnapshot(
                repo=repo,
                number=int(i["number"]),
                title=i.get("title") or "",
                body=i.get("body") or "",
                author=_login(i.get("user")),
                labels=[
                    label["name"]
                    for label in i.get("labels") or []
                    if isinstance(label, dict)
                ],
                created_at=_dt(i.get("created_at")),
                url=i.get("html_url") or "",
            )
            for i in items
            if "pull_request" not in i
        ]

    async def list_merged_prs(
        self, repo: str, limit: int = 50
    ) -> list[MergedPrSummary]:
        owner, name = _split(repo)
        data = await self._client.graphql(
            _MERGED_QUERY,
            {"owner": owner, "name": name, "count": max(1, min(limit, 100))},
            repo=repo,
        )
        nodes = _nodes(data["repository"]["pullRequests"])
        out = [
            MergedPrSummary(
                number=int(n["number"]),
                title=n.get("title") or "",
                author=_login(n.get("author")),
                labels=[label["name"] for label in _nodes(n.get("labels"))],
                merged_at=_dt(n.get("mergedAt")),
                commit_messages=[
                    (c.get("commit") or {}).get("message") or ""
                    for c in _nodes(n.get("commits"))
                ],
                review_count=int((n.get("reviews") or {}).get("totalCount") or 0),
            )
            for n in nodes
        ]
        return sorted(
            out,
            key=lambda p: p.merged_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    # ------------------------------------------------------------------ contents

    async def get_file(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        data = await self._client.get(
            f"/repos/{repo}/contents/{quote(path.lstrip('/'))}",
            repo=repo,
            params={"ref": ref} if ref else None,
            allow_404=True,
        )
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        if data.get("encoding") == "base64" and data.get("content") is not None:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        raw = await self._client.request(
            "GET",
            f"/repos/{repo}/contents/{quote(path.lstrip('/'))}",
            repo=repo,
            params={"ref": ref} if ref else None,
            headers={"Accept": "application/vnd.github.raw"},
        )
        return raw.text if raw.status_code == 200 else None

    async def get_pr_diff(self, repo: str, number: int) -> str | None:
        raw = await self._client.request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            repo=repo,
            headers={"Accept": "application/vnd.github.diff"},
        )
        return raw.text if raw.status_code == 200 else None

    async def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[str]:
        data = await self._client.get(
            f"/repos/{repo}/contents/{quote(path.strip('/'))}",
            repo=repo,
            params={"ref": ref} if ref else None,
            allow_404=True,
        )
        if not isinstance(data, list):
            return []
        return [entry["name"] for entry in data if entry.get("name")]

    async def path_changed_by(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        params: dict[str, Any] = {"path": path, "per_page": 1}
        if ref:
            params["sha"] = ref
        commits = await self._client.get(
            f"/repos/{repo}/commits", repo=repo, params=params, allow=(404, 409)
        )
        if not commits:
            return None
        commit = commits[0]
        sha = commit["sha"]
        pulls = (
            await self._client.get(
                f"/repos/{repo}/commits/{sha}/pulls", repo=repo, allow=(404, 422)
            )
            or []
        )
        for pull in pulls:
            if not pull.get("merged_at"):
                continue
            merged_by = (pull.get("merged_by") or {}).get("login")
            if not merged_by:
                detail = await self._client.get(
                    f"/repos/{repo}/pulls/{pull['number']}", repo=repo, allow_404=True
                )
                merged_by = ((detail or {}).get("merged_by") or {}).get("login")
            if merged_by:
                return merged_by
        committer = (commit.get("committer") or {}).get("login")
        if committer and committer != "web-flow":
            return committer
        return (commit.get("author") or {}).get("login")

    # ------------------------------------------------------------------ people

    async def get_permission(self, repo: str, login: str) -> Permission:
        data = await self._client.get(
            f"/repos/{repo}/collaborators/{quote(login, safe='')}/permission",
            repo=repo,
            allow=(403, 404),
        )
        if not data:
            return Permission.NONE
        return Permission.parse(data.get("role_name") or data.get("permission"))

    async def is_team_member(self, repo: str, team: str, login: str) -> bool:
        org, _, team_slug = team.partition("/")
        if not team_slug:
            return False
        try:
            data = await self._client.get(
                f"/orgs/{org}/teams/{team_slug}/memberships/{quote(login, safe='')}",
                repo=org,
                allow=(403, 404),
            )
        except GitHubError:
            return False
        return bool(data) and data.get("state") == "active"
