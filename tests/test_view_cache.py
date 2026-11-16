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

"""The dashboard's views read a repository's settings and CODEOWNERS from the forge
once for many PRs, not once per PR shown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from auto_maintainer.testing.demo import build_demo


async def test_many_views_at_once_read_the_forge_once(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        reads: list[str] = []
        get_repo, get_file = h.forge.get_repo, h.forge.get_file

        async def counted_repo(repo: str) -> Any:
            reads.append("repo")
            await asyncio.sleep(0.01)
            return await get_repo(repo)

        async def counted_file(repo: str, path: str, ref: str | None = None) -> Any:
            reads.append(f"file:{path}")
            return await get_file(repo, path, ref)

        h.forge.get_repo = counted_repo  # type: ignore[method-assign]
        h.forge.get_file = counted_file  # type: ignore[method-assign]
        await asyncio.gather(*(h.service.view_repo("acme/widget") for _ in range(20)))
        assert reads == ["repo"], "one read for the views that asked at once"
        await h.service.view_repo("acme/widget")
        await h.service.view_file("acme/widget", ".github/CODEOWNERS", "main")
        await h.service.view_file("acme/widget", ".github/CODEOWNERS", "main")
        assert reads == ["repo", "file:.github/CODEOWNERS"]
        h.clock.advance(minutes=6)
        await h.service.view_repo("acme/widget")
        assert reads.count("repo") == 2, "read again once it is old"
    finally:
        await h.close()


async def test_pr_list_batches_approver_permissions_and_negative_codeowners(
    tmp_path: Path,
) -> None:
    import httpx

    from auto_maintainer.domain.permissions import Permission
    from auto_maintainer.domain.snapshot import Review, ReviewEventState
    from auto_maintainer.testing.fakes import make_pr
    from auto_maintainer.web.app import create_app

    h = await build_demo(tmp_path)
    try:
        # Remove CODEOWNERS so the repository has none at any of the four paths.
        for key in [k for k in h.forge.files if "CODEOWNERS" in k[1]]:
            del h.forge.files[key]
        for idx, login in enumerate(("rev_a", "rev_b", "rev_c"), start=100):
            h.forge.set_permission("acme/widget", login, Permission.WRITE)
            pr = make_pr(
                repo="acme/widget",
                number=idx,
                reviews=[
                    Review(
                        id=idx,
                        author=login,
                        state=ReviewEventState.APPROVED,
                        commit_id="a" * 40,
                        submitted_at=h.clock.now(),
                    )
                ],
            )
            h.forge.put_pr(pr)
            await h.store.save_snapshot(pr)

        file_reads: list[str] = []
        perm_inflight = 0
        max_inflight = 0
        perm_calls: list[str] = []
        orig_file = h.forge.get_file
        orig_perm = h.forge.get_permission

        async def counted_file(repo: str, path: str, ref: str | None = None) -> Any:
            file_reads.append(path)
            return await orig_file(repo, path, ref)

        async def counted_perm(repo: str, login: str) -> Permission:
            nonlocal perm_inflight, max_inflight
            perm_calls.append(login)
            perm_inflight += 1
            max_inflight = max(max_inflight, perm_inflight)
            await asyncio.sleep(0.02)
            perm_inflight -= 1
            return await orig_perm(repo, login)

        h.forge.get_file = counted_file  # type: ignore[method-assign]
        h.forge.get_permission = counted_perm  # type: ignore[method-assign]
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/auth/dev?login=maria")
            first = await client.get("/api/prs")
            assert first.status_code == 200
            reads_after_first = list(file_reads)
            second = await client.get("/api/prs")
            assert second.status_code == 200
        assert max_inflight >= 3, "approvers' expired permissions read in parallel"
        assert file_reads == reads_after_first, "missing CODEOWNERS is cached"
        assert perm_calls.count("rev_a") == 1, "deduplicated and cached"
    finally:
        await h.close()
