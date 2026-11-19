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
        await asyncio.sleep(0.05)
        assert reads.count("repo") == 2, "read again once it is old"
        h.clock.advance(minutes=11)
        await h.service.view_repo("acme/widget")
        await asyncio.sleep(0.05)
        assert reads.count("repo") == 3, "shown at once while it is read again"
        h.clock.advance(minutes=31)
        await h.service.view_repo("acme/widget")
        assert reads.count("repo") == 4, "too old to show: waited for"
    finally:
        await h.close()


async def test_a_page_does_not_wait_for_what_it_showed_a_moment_ago(
    tmp_path: Path,
) -> None:
    """A kept value past its window is shown at once while the forge is read again;
    a read that began before a change dropped the views keeps nothing."""
    h = await build_demo(tmp_path)
    try:
        first = await h.service.view_repo("acme/widget")
        get_repo = h.forge.get_repo
        release = asyncio.Event()

        async def slow_repo(repo: str) -> Any:
            await release.wait()
            return await get_repo(repo)

        h.forge.get_repo = slow_repo  # type: ignore[method-assign]
        h.clock.advance(minutes=6)
        shown = await asyncio.wait_for(h.service.view_repo("acme/widget"), 1)
        assert shown is first, "shown at once while it is read again"
        h.service.forget_views("repo")
        release.set()
        await asyncio.sleep(0.05)
        assert ("repo", "acme/widget") not in h.service._views, (
            "a read that began before the drop keeps nothing"
        )
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


async def test_who_maintains_is_kept_for_views_until_the_record_changes(
    tmp_path: Path,
) -> None:
    """Pages show maintainers' controls from a kept check, so a page that reloads often
    does not read the person's permission from the forge each time; a change to the
    memory repo (a People record) drops it, so the next page shows the change."""
    import yaml

    from auto_maintainer.domain.events import Event, EventKind
    from auto_maintainer.domain.permissions import Permission
    from auto_maintainer.profiles.governance import governance_path
    from auto_maintainer.testing.demo import DEMO_GOVERNANCE
    from auto_maintainer.web.api import shows_maintainer
    from auto_maintainer.web.auth import User

    h = await build_demo(tmp_path)
    try:
        reads: list[str] = []
        get_permission = h.forge.get_permission

        async def counted(repo: str, login: str) -> Permission:
            reads.append(login)
            return await get_permission(repo, login)

        h.forge.get_permission = counted  # type: ignore[method-assign]
        bob = User(login="bob")
        assert not await shows_maintainer(h.service, bob, "acme/widget")
        assert not await shows_maintainer(h.service, bob, "acme/widget")
        assert reads == ["bob"], "kept for the views"

        [maria] = [m for m in DEMO_GOVERNANCE["members"] if m["login"] == "maria"]
        record = {
            **DEMO_GOVERNANCE,
            "members": [*DEMO_GOVERNANCE["members"], {**maria, "login": "bob"}],
        }
        h.memory.files[governance_path("acme/widget")] = yaml.safe_dump(record)
        assert not await shows_maintainer(h.service, bob, "acme/widget"), "still kept"
        await h.emit(
            Event(
                kind=EventKind.MEMORY_CHANGED, repo="acme/widget", payload={"paths": []}
            )
        )
        await h.settle()
        assert await shows_maintainer(h.service, bob, "acme/widget")
    finally:
        await h.close()


async def test_a_kept_permission_is_read_again_before_it_runs_out(
    tmp_path: Path,
) -> None:
    """A view never shows a permission older than ``PERMISSION_TTL``. In its last
    minutes a view still shows the kept one at once and it is read again in the
    background, so a page people keep open never waits for the forge; past it, the
    view reads it first."""
    from auto_maintainer.domain.permissions import Permission
    from auto_maintainer.web.api import permission
    from auto_maintainer.web.auth import User

    h = await build_demo(tmp_path)
    try:
        bob = User(login="bob")
        assert await permission(h.service, bob, "acme/widget") == Permission.WRITE
        h.forge.set_permission("acme/widget", "bob", Permission.READ)
        h.clock.advance(minutes=8)
        release = asyncio.Event()
        get_permission = h.forge.get_permission

        async def slow(repo: str, login: str) -> Permission:
            await release.wait()
            return await get_permission(repo, login)

        h.forge.get_permission = slow  # type: ignore[method-assign]
        shown = await asyncio.wait_for(permission(h.service, bob, "acme/widget"), 1)
        assert shown == Permission.WRITE, "the kept one, at once"
        release.set()
        for _ in range(20):
            if not h.service._permission_reads:
                break
            await asyncio.sleep(0.01)
        assert await permission(h.service, bob, "acme/widget") == Permission.READ

        h.forge.set_permission("acme/widget", "bob", Permission.NONE)
        h.clock.advance(minutes=31)
        assert await permission(h.service, bob, "acme/widget") == Permission.NONE, (
            "never older than thirty minutes"
        )
    finally:
        await h.close()


async def test_an_agents_sign_in_shows_at_once_while_its_cli_is_asked_again(
    tmp_path: Path,
) -> None:
    """Asking a CLI whether it is signed in starts it in a container: a page shows
    the kept answer while it is asked again, and waits only for one never asked or
    older than ``VIEW_STALE_LIMIT``."""
    h = await build_demo(tmp_path)
    try:
        answers = iter([True, False, True])
        release = asyncio.Event()
        asked: list[Any] = []

        class Runtime:
            provider = None

            async def signed_in(self, home: Any) -> bool:
                asked.append(home)
                if len(asked) == 2:
                    await release.wait()
                return next(answers)

        runtime = Runtime()
        h.service.runtime = lambda name, account=None: runtime  # type: ignore[method-assign]
        h.service.agent_ready = lambda name: True  # type: ignore[method-assign]
        assert await h.service.signed_in("codex") is True, "never asked: waited for"
        h.clock.advance(minutes=2)
        shown = await asyncio.wait_for(h.service.signed_in("codex"), 1)
        await asyncio.sleep(0.01)
        assert shown is True and len(asked) == 2, "the kept answer, at once"
        assert await h.service.signed_in("codex") is True and len(asked) == 2, (
            "one CLI at a time"
        )
        release.set()
        await asyncio.sleep(0.05)
        assert await h.service.signed_in("codex") is False, "the new answer next"
        h.clock.advance(minutes=31)
        assert await h.service.signed_in("codex") is True, "too old to show"
    finally:
        await h.close()


async def test_a_start_warms_what_the_first_pages_would_wait_for(
    tmp_path: Path,
) -> None:
    """Soon after a start, the service reads each repository's settings and the
    roles of the people it served in the last day, so their first pages after a
    deploy do not wait on the forge."""
    from auto_maintainer.web.api import permission
    from auto_maintainer.web.auth import User

    h = await build_demo(tmp_path)
    try:
        await permission(h.service, User(login="bob"), "acme/widget")
        h.service._views.clear()  # as a new process starts
        reads: list[str] = []
        get_repo = h.forge.get_repo

        async def counted(repo: str) -> Any:
            reads.append(repo)
            return await get_repo(repo)

        h.forge.get_repo = counted  # type: ignore[method-assign]
        await h.service.warm_views(delay=0)
        assert "acme/widget" in reads
        assert ("access", "acme/widget", "bob") in h.service._views
        before = len(reads)
        await h.service.view_repo("acme/widget")
        assert len(reads) == before, "the first page finds it read"
    finally:
        await h.close()
