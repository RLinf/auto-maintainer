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

"""How the service is reached and run is for those who run it (``web/api.py:
sees_internals``): its owner, operators, and its repositories' maintainers. Other
members see what they need to use it."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.settings import parse_settings
from auto_maintainer.testing.demo import DEMO_REPO, build_demo
from auto_maintainer.web.app import create_app


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        yield client


async def test_members_see_machines_by_name_and_maintainers_see_how_to_reach_them(
    tmp_path: Path,
) -> None:
    h = await build_demo(tmp_path)
    try:
        h.forge.set_permission(DEMO_REPO, "walt", Permission.WRITE)
        h.service.service_config = h.service.service_config.model_copy(
            update={
                "providers": parse_settings(
                    "providers: {gw: {protocol: openai, "
                    "base_url: 'https://gw.internal/v1', models: [m]}}"
                ).providers
            }
        )
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with _as(app, "walt") as walt:
            seen = (await walt.get("/api/settings")).json()
            assert seen["document"] is None, "not the raw file"
            [lab] = [m for m in seen["labs"] if m["name"] == "gpu-01"]
            assert "host" not in lab and "user" not in lab and "port" not in lab
            assert "description" in lab, "what the machine is, to choose it"
            [gateway] = seen["providers"]
            assert "base_url" not in gateway, "not where the model API is"
            offered = (await walt.get(f"/api/labs?repo={DEMO_REPO}")).json()
            assert all(m["host"] is None for m in offered["machines"])
        async with _as(app, "maria") as maria:
            seen = (await maria.get("/api/settings")).json()
            assert seen["document"] is not None
            [lab] = [m for m in seen["labs"] if m["name"] == "gpu-01"]
            assert lab["host"] == "ci@gpu-01", "a maintainer runs it"
            assert seen["providers"][0]["base_url"] == "https://gw.internal/v1"
            offered = (await maria.get(f"/api/labs?repo={DEMO_REPO}")).json()
            assert any(m["host"] for m in offered["machines"])
    finally:
        await h.close()


async def test_the_api_description_is_for_signed_in_people(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as anyone:
            assert (await anyone.get("/api/openapi.json")).status_code == 401
            assert (await anyone.get("/api/docs")).status_code == 401
        async with _as(app, "maria") as maria:
            described = await maria.get("/api/openapi.json")
            assert (
                described.status_code == 200 and "/api/prs" in described.json()["paths"]
            )
            assert (await maria.get("/api/docs")).status_code == 200
    finally:
        await h.close()
