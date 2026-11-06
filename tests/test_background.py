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

"""Running in the background: the supervisor's log file, starting and stopping, and
reading the log from the terminal and the dashboard."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import pytest

from auto_maintainer import background
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.selfupdate.supervise import LogFile, supervise
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


def test_the_supervisor_writes_what_the_service_prints_to_its_log(
    tmp_path: Path,
) -> None:
    log = tmp_path / "logs" / "service.log"
    script = (
        "import sys; print('hello from serve'); print('a warning', file=sys.stderr)"
    )
    code = supervise(
        [sys.executable, "-c", script], tmp_path, tmp_path / "deploy.json", log_file=log
    )
    assert code == 0
    text = log.read_text()
    assert "hello from serve" in text and "a warning" in text


def test_the_log_rotates_by_size(tmp_path: Path) -> None:
    log = LogFile(tmp_path / "service.log", max_bytes=200, backups=2)
    for i in range(40):
        log.write(f"line {i:03d} " + "x" * 20)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["service.log", "service.log.1", "service.log.2"]
    assert "line 039" in (tmp_path / "service.log").read_text()


def test_tail_and_follow_read_the_log_across_a_rotation(tmp_path: Path) -> None:
    path = tmp_path / "service.log"
    path.write_text("one\ntwo\nthree\n")
    assert background.tail(path, 2) == ["two", "three"]
    lines = background.follow(path, poll=0.01)
    with path.open("a") as f:
        f.write("four\n")
    assert next(lines) == "four"
    path.rename(tmp_path / "service.log.1")
    path.write_text("five\n")
    assert next(lines) == "five"


def test_start_runs_in_the_background_and_stop_ends_it(tmp_path: Path) -> None:
    script = "import auto_maintainer, time; print('up', flush=True); time.sleep(60)"
    pid = background.start([sys.executable, "-c", script], tmp_path, tmp_path)
    try:
        for _ in range(50):
            if background.running(tmp_path) == pid:
                break
            time.sleep(0.1)
        assert background.running(tmp_path) == pid
        with pytest.raises(RuntimeError, match="already runs"):
            background.start([sys.executable, "-c", script], tmp_path, tmp_path)
    finally:
        assert background.stop(tmp_path, timeout=10) is True
    assert background.running(tmp_path) is None
    assert background.stop(tmp_path) is False


async def test_service_maintainers_read_the_log_without_secrets(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/auto-maintainer", "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/auth/dev?login=maria")
            empty = (await client.get("/api/service/logs")).json()
            assert empty["kept"] is False
            secret = "ghp_" + "z" * 36
            log = tmp_path / "logs" / "service.log"
            log.parent.mkdir(parents=True)
            log.write_text(f"INFO started\nERROR token {secret} leaked\n")
            out = (await client.get("/api/service/logs?lines=10")).json()
            assert out["kept"] and out["lines"][0] == "INFO started"
            assert secret not in out["lines"][1] and "ERROR token" in out["lines"][1]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as other:
            await other.get("/auth/dev?login=walt")
            assert (await other.get("/api/service/logs")).status_code == 403
    finally:
        await h.close()
