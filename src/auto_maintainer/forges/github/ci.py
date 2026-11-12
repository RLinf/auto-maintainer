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

"""``CiProvider`` for GitHub Actions."""

from __future__ import annotations

import re

import httpx

from ...domain.snapshot import RunnerInfo
from .client import GitHubClient, GitHubError

MAX_LOG_BYTES = 2 * 1024 * 1024


READTHEDOCS_BUILD = re.compile(
    r"^https://(?:app\.)?readthedocs\.(?:org|com)/projects/[A-Za-z0-9_.-]+/builds/(\d+)/?$"
)
"""A Read the Docs build's page, as its commit status links to it."""


class GitHubActionsCi:
    """Job logs, reruns, and runner status from GitHub Actions.

    Args:
        client: The API client.
        download: The client used to fetch log archives from the signed URL GitHub
            redirects to. It must not carry GitHub credentials.
    """

    def __init__(
        self, client: GitHubClient, *, download: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client
        self._download = download or httpx.AsyncClient(
            trust_env=True, timeout=60.0, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._download.aclose()

    async def get_job_log(self, repo: str, job_id: int) -> str | None:
        """The job's log, cut to its last ``MAX_LOG_BYTES``; ``None`` when it is gone."""
        resp = await self._client.request(
            "GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", repo=repo
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return None
            resp = await self._download.get(location)
        if resp.status_code != 200:
            return None
        data = resp.content[-MAX_LOG_BYTES:]
        return data.decode("utf-8", errors="replace")

    async def get_status_log(self, repo: str, url: str) -> str | None:
        """The log of a Read the Docs build a commit status links to: each command
        of the build with the output of those that failed, cut to the last
        ``MAX_LOG_BYTES``. Only Read the Docs' own API is asked, for the build the
        link names: the link itself, set by whoever reported the status, is never
        fetched. ``None`` for any other link."""
        found = READTHEDOCS_BUILD.match(url or "")
        if found is None:
            return None
        resp = await self._download.get(
            f"https://readthedocs.org/api/v2/build/{found.group(1)}/",
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        build = resp.json()
        lines = [
            f"Read the Docs build {found.group(1)}: "
            + ("succeeded" if build.get("success") else "failed")
            + (f" ({build['error']})" if build.get("error") else "")
        ]
        failed_git = False
        for command in build.get("commands") or []:
            code = command.get("exit_code")
            run = str(command.get("command") or "")
            lines.append(f"$ {run}  (exit {code})")
            if code:
                lines.append(str(command.get("output") or ""))
                failed_git = failed_git or run.startswith("git ")
        if failed_git and not build.get("success"):
            stale = await self._stale_pull_ref(repo, build)
            if stale:
                lines.append(stale)
        text = "\n".join(lines)
        return text[-MAX_LOG_BYTES:]

    async def _stale_pull_ref(self, repo: str, build: dict) -> str | None:
        """Why a pull request's Read the Docs build could not check out its commit,
        when GitHub has not moved ``refs/pull/<n>/head`` to it: Read the Docs builds
        a PR by fetching that ref, then checking out the commit the status is for."""
        slug = str(build.get("version_slug") or "")
        commit = str(build.get("commit") or "")
        if build.get("version_type") != "external" or not slug.isdigit() or not commit:
            return None
        try:
            ref = await self._client.get(
                f"/repos/{repo}/git/ref/pull/{slug}/head", repo=repo, allow_404=True
            )
        except (GitHubError, httpx.HTTPError):  # the log stands without the note
            return None
        at = str(((ref or {}).get("object") or {}).get("sha") or "")
        if not at or at == commit:
            return None
        return (
            f"The build failed before it built anything: GitHub's refs/pull/{slug}/head "
            f"is still at {at[:10]}, not at {commit[:10]}, the PR's head this build "
            "is for, so the commit is missing from what Read the Docs fetched. "
            "Nothing in the pull request's files caused this failure, and no change "
            "to them fixes it. GitHub moves that ref when the PR's branch gets a new "
            "push; after that, Read the Docs builds the new head."
        )

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        await self._client.post(
            f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs", repo=repo
        )

    async def list_runners(self, repo: str) -> list[RunnerInfo]:
        data = await self._client.get(
            f"/repos/{repo}/actions/runners",
            repo=repo,
            params={"per_page": 100},
            allow=(403, 404),
        )
        return [
            RunnerInfo(
                name=r.get("name") or "",
                status=r.get("status") or "unknown",
                busy=bool(r.get("busy")),
                labels=[
                    label.get("name")
                    for label in r.get("labels") or []
                    if label.get("name")
                ],
            )
            for r in (data or {}).get("runners", [])
        ]
