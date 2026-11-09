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

"""Fixing a PR's failing CI (design 5.5): an agent changes a checkout of the PR,
tests its change on a test machine through its session (``lab --sync``, then
``lab -- ...``), and the fix comes back as a draft its requester pushes as
themselves."""

from __future__ import annotations

import base64
import contextlib
import io
import subprocess
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.actions import LabReproducePayload
from auto_maintainer.domain.agents import AgentResult, AgentTask
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.ports import CommandResult
from auto_maintainer.domain.profile import LabBinding
from auto_maintainer.domain.settings import LabMachine
from auto_maintainer.labs.executor import LabExecutor
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import FakeLabHost
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


def _fixer(task: AgentTask) -> Any:
    """The demo agent: edits the checkout, then describes its diff."""
    if task.mode == "read_only":
        return {
            "title": "fix(ui): count the step before drawing",
            "summary": "The bar counts a finished step before it draws.",
            "fixed": [{"id": "unit", "change": "count before drawing"}],
        }
    (task.workdir / "src").mkdir(parents=True, exist_ok=True)
    (task.workdir / "src" / "ui.py").write_text("done += 1\ndraw()\n")
    return AgentResult(
        runtime="scripted",
        ok=True,
        text="unit failed because the bar drew first; counted first; it passes.",
    )


async def test_an_agent_fixes_failing_ci_and_tests_it_on_a_machine(demo) -> None:
    h, app = demo
    runner = h.service.a.lab_runner
    h.runner.on("fix", _fixer)
    async with _as(app, "maria") as maria:
        none = await maria.post(f"/api/prs/{REPO}/42/fix-ci", json={})
        assert none.status_code == 409, "PR 42's CI passed"
        out = (
            await maria.post(
                f"/api/prs/{REPO}/43/fix-ci",
                json={"lab": "gpu-01", "note": "the progress bar"},
            )
        ).json()
        assert out["session"]["status"] in ("queued", "executed"), out
        await h.settle()

        [opened] = runner.reproduced
        assert set(opened["code"]) == {"head"}, "only the PR's head is copied"
        [work, describe] = [t for t in h.runner.tasks if t.kind == "fix"]
        assert work.lab_session == f"token-{opened['session']}"
        assert "lab --sync" in work.prompt and "unit: failure (run 11)" in work.prompt
        assert "the progress bar" in work.prompt and work.requested_by == "maria"
        assert runner.synced[opened["session"]] == str(work.workdir)
        assert describe.mode == "read_only" and describe.lab_session is None
        assert runner.closed == [opened["session"]], "the session closes after"

        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "pr.fix"
        ]
        payload = draft["action"]["action"]["payload"]
        assert draft["requested_by"] == "maria", "she pushes it as herself"
        assert payload["files"] == ["src/ui.py"] and payload["findings"] == ["unit"]
        assert payload["title"] == "fix(ui): count the step before drawing"


async def test_without_a_machine_the_agent_runs_nothing(demo) -> None:
    h, app = demo
    h.runner.on("fix", _fixer)
    async with _as(app, "maria") as maria:
        out = (await maria.post(f"/api/prs/{REPO}/43/fix-ci", json={})).json()
        assert out["session"] is None
        await h.settle()
    [work, _] = [t for t in h.runner.tasks if t.kind == "fix"]
    assert work.lab_session is None and "none was chosen" in work.prompt
    assert h.service.a.lab_runner.reproduced == []


async def test_sync_copies_the_agents_checkout_and_removes_what_it_deleted(
    tmp_path: Path,
) -> None:
    from auto_maintainer.store.sqlite import SqliteStore

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for path, text in {
        "keep.py": "a\n",
        "gone.py": "b\n",
        ".gitignore": "*.log\n",
    }.items():
        (checkout / path).write_text(text)
    git = ["git", "-C", str(checkout)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    (checkout / "gone.py").unlink()
    (checkout / "new.py").write_text("c\n")
    (checkout / "debug.log").write_text("ignored\n")

    host = FakeLabHost("gpu-01")
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"gpu-01": LabMachine()})
    labs.hosts["gpu-01"] = host
    opened = await labs.open_session(
        LabReproducePayload(lab="gpu-01", summary="s", sides=["head"]),
        REPO,
        43,
        {"head": ""},
        "act_12345678",
        binding=LabBinding(),
    )
    token = labs.session_token(opened["session"])
    assert token
    assert "error" in await labs.sync_session(token), "nothing to copy until allowed"
    labs.allow_sync(opened["session"], str(checkout))
    out = await labs.sync_session(token)
    assert out == {"exit_code": 0, "files": 3, "removed": 1}
    command = host.commands[-1][0]
    assert command.startswith("cd /home/ci/am-labs/acme__widget-43-12345678/head")
    assert "rm -f -- gone.py" in command
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(host.stdin[-1]))) as tar:
        assert sorted(tar.getnames()) == [".gitignore", "keep.py", "new.py"]
