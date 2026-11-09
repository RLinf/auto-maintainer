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

"""``CiProvider`` for GitLab CI (design 5.16): a check is a pipeline job, a run is
its pipeline."""

from __future__ import annotations

from ...domain.snapshot import RunnerInfo
from .client import GitLabClient, project_id


class GitLabCi:
    """Implements ``CiProvider`` for one GitLab forge."""

    def __init__(self, client: GitLabClient) -> None:
        self._client = client

    async def get_job_log(self, repo: str, job_id: int) -> str | None:
        return await self._client.get(
            f"/projects/{project_id(repo)}/jobs/{job_id}/trace",
            allow=(401, 403, 404),
            raw=True,
        )

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        """Retry the pipeline's failed jobs (GitLab's retry runs only those)."""
        await self._client.post(
            f"/projects/{project_id(repo)}/pipelines/{run_id}/retry"
        )

    async def list_runners(self, repo: str) -> list[RunnerInfo]:
        runners = await self._client.paginate(
            f"/projects/{project_id(repo)}/runners", allow=(401, 403, 404)
        )
        return [
            RunnerInfo(
                name=str(r.get("description") or r.get("name") or r.get("id")),
                status=str(r.get("status") or ""),
                busy=str(r.get("status")) == "running",
                labels=[str(t) for t in r.get("tag_list") or []],
            )
            for r in runners
        ]
