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

"""Who may do what, on the dashboard (``web/access.py``, design 2.7): each person
reads their own permissions; the owner and service admins change
``config/access.yaml``, and only the owner names the service admins."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.testing.demo import DEMO_REPO, build_demo
from auto_maintainer.web.app import create_app


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_people_read_their_roles_and_the_owner_names_service_admins(
    tmp_path: Path,
) -> None:
    h = await build_demo(tmp_path)
    try:
        h.forge.set_permission(DEMO_REPO, "walt", Permission.WRITE)
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with _as(app, "maria") as maria, _as(app, "walt") as walt:
            [mine] = (await maria.get("/api/access/me")).json()["repos"]
            assert mine["role"] == "maintainer" and "merge.schedule" in mine["grants"]
            [theirs] = (await walt.get("/api/access/me")).json()["repos"]
            assert theirs["role"] == "reviewer" and "use.agents" in theirs["grants"]
            assert (await walt.get("/api/access")).status_code == 403

            bad = await maria.put(
                "/api/access", json={"text": "people: {x: {role: king}}"}
            )
            assert bad.status_code == 422
            out = await maria.put(
                "/api/access",
                json={"text": "admins: [walt]\npeople:\n  bob: {role: blocked}\n"},
            )
            assert out.status_code == 200 and out.json()["applied"], out.text
            assert h.service.access.blocked("bob")
            assert (await walt.get("/api/access")).status_code == 200, "now an admin"

            # A service admin changes people, but not who the admins are.
            renamed = await walt.put(
                "/api/access", json={"text": "admins: [walt, zed]\n"}
            )
            assert renamed.status_code == 200 and not renamed.json()["applied"]
            assert "owner names" in renamed.json()["reason"]
            kept = await walt.put(
                "/api/access",
                json={"text": "admins: [walt]\npeople:\n  bob: {role: viewer}\n"},
            )
            assert kept.json()["applied"], kept.text
            assert not h.service.access.blocked("bob")
        async with _as(app, "bob") as bob:
            assert (await bob.get(f"/api/prs/{DEMO_REPO}/42")).status_code == 200
            await maria_blocks(app, "bob")
            assert (await bob.get(f"/api/prs/{DEMO_REPO}/42")).status_code == 403
            assert (await bob.get("/api/access/me")).json()["repos"] == [
                {
                    "repo": DEMO_REPO,
                    "role": "blocked",
                    "role_from": "set for them",
                    "grants": {},
                    "withheld": {},
                    "limits": {
                        "tokens_per_day": None,
                        "tokens_per_week": None,
                        "jobs_at_once": None,
                        "agents": None,
                        "machines": None,
                    },
                }
            ]
        assert await h.service.admits("bob") is False, "no sign-in either"
    finally:
        await h.close()


async def maria_blocks(app: Any, login: str) -> None:
    async with _as(app, "maria") as maria:
        out = await maria.put(
            "/api/access",
            json={"text": f"admins: [walt]\npeople:\n  {login}: {{role: blocked}}\n"},
        )
        assert out.json()["applied"], out.text
