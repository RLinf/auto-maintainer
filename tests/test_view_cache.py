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
