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

"""A forge's webhook makes the service read the repository at once, without a
delivery waiting on the read or two reads of one repository running together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from auto_maintainer.forges.github.webhooks import hint, verify_signature
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.harness import Harness
from auto_maintainer.web.app import WebhookCheck, create_app

REPO = "acme/widget"


async def test_deliveries_poll_at_once_and_again_after_a_running_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AM_WEBHOOK_SECRET", "s3cret")
    h = await build_demo(tmp_path)
    try:
        polls: list[str] = []
        release = asyncio.Event()
        poll_repo = h.service.poller.poll_repo

        async def slow_poll(repo: str, reconcile: bool = False):  # type: ignore[no-untyped-def]
            polls.append(repo)
            await release.wait()
            return await poll_repo(repo, reconcile)

        h.service.poller.poll_repo = slow_poll  # type: ignore[method-assign]
        app = create_app(
            h.service,
            data_dir=tmp_path,
            webhooks={"github": WebhookCheck(verify_signature, hint)},
        )
        body = json.dumps(
            {"repository": {"full_name": REPO}, "pull_request": {"number": 42}}
        ).encode()
        signed = {
            "X-Hub-Signature-256": "sha256="
            + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "pull_request",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as forge:
            forged = await forge.post("/webhooks/github", content=body)
            assert forged.status_code == 401
            first = await asyncio.wait_for(
                forge.post("/webhooks/github", content=body, headers=signed), 1
            )
            assert first.json() == {"poll": "started"}, "answered before the poll ends"
            for _ in range(3):
                more = await forge.post(
                    "/webhooks/github", content=body, headers=signed
                )
                assert more.json() == {"poll": "after the one running"}
            release.set()
            for _ in range(50):
                task = h.service._polls.get(REPO)
                if task is not None and task.done():
                    break
                await asyncio.sleep(0.01)
        assert polls == [REPO, REPO], (
            "one more poll for every delivery during the first"
        )
    finally:
        await h.close()


async def test_polls_of_one_repository_never_run_together(tmp_path: Path) -> None:
    """Two polls of one repository at once (the schedule and a webhook) would both
    see a change against the same stored snapshot and publish it twice: the second
    waits for the first. Polls of different repositories do not wait."""
    h = await Harness.create(tmp_path)
    try:
        steps: list[str] = []
        inner = h.service.poller._poll_repo

        async def traced(repo: str, reconcile: bool):  # type: ignore[no-untyped-def]
            steps.append(f"start {repo}")
            await asyncio.sleep(0.02)
            steps.append(f"end {repo}")
            return await inner(repo, reconcile)

        h.service.poller._poll_repo = traced  # type: ignore[method-assign]
        await asyncio.gather(
            h.service.poller.poll_repo(REPO),
            h.service.poller.poll_repo(REPO),
            h.service.poller.poll_repo("acme/other"),
        )
        mine = [s for s in steps if s.endswith(REPO)]
        assert mine == [f"start {REPO}", f"end {REPO}"] * 2
        assert steps.index("start acme/other") < steps.index(f"end {REPO}")
    finally:
        await h.close()
