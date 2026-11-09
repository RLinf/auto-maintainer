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

"""Reading a review in the dashboard's language: a translation for the reader only,
kept per text and language; what is posted does not change."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_review_draft_is_translated_for_reading_only(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "review.post"
        ]
        action_id = draft["action_id"]
        before = (await h.store.get_action(action_id)).action.payload  # type: ignore[union-attr]
        url = f"/api/actions/{action_id}/translate"
        out = (await maria.post(url, json={"to": "zh"})).json()
        assert out["body"].startswith("*(中文)*") and not out["cached"]
        assert len(out["comments"]) == len(before.get("comments") or [])
        [task] = [t for t in h.runner.tasks if t.kind == "translate"]
        assert task.untrusted_input and task.mode == "read_only"
        assert "into Simplified Chinese" in task.prompt
        assert before["body"][:80] in task.prompt
        again = (await maria.post(url, json={"to": "zh"})).json()
        assert again["cached"] and again["body"] == out["body"]
        assert len([t for t in h.runner.tasks if t.kind == "translate"]) == 1
        assert (await maria.post(url, json={"to": "fr"})).status_code == 422
        after = (await h.store.get_action(action_id)).action.payload  # type: ignore[union-attr]
        assert after == before, "what is posted stays as it is"
    async with _as(app, "eve") as eve:
        assert (await eve.post(url, json={"to": "zh"})).status_code == 403
