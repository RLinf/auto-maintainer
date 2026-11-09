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

import httpx

from ...domain.snapshot import RunnerInfo
from .client import GitHubClient

MAX_LOG_BYTES = 2 * 1024 * 1024


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
