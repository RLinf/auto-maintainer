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

"""Stopping: an open dashboard never holds the server up, and the supervisor never
leaves the service running behind it."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx

from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.engine.bus import EventBus
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app


async def test_a_live_stream_beats_while_idle_and_ends_when_closing() -> None:
    bus = EventBus()
    closing = asyncio.Event()
    stream = bus.stream(closing, heartbeat=0.05)
    assert await asyncio.wait_for(anext(stream), 1) is None
    await bus.publish(Event(kind=EventKind.MEMORY_CHANGED, payload={}))
    got = await asyncio.wait_for(anext(stream), 1)
    assert got is not None and got.kind == EventKind.MEMORY_CHANGED
    closing.set()
    async with asyncio.timeout(1):
        assert [e async for e in stream] == []
    await bus.stop()


async def test_the_event_stream_closes_when_the_service_stops(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/auth/dev?login=maria")
        asyncio.get_running_loop().call_later(0.2, h.service.closing.set)
        async with asyncio.timeout(5):
            res = await client.get("/api/events")
    assert res.status_code == 200 and res.text.startswith("retry:")
    await h.close()


def _run_supervisor(tmp_path: Path, child_code: str, grace: float) -> subprocess.Popen:
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(child_code), encoding="utf-8")
    runner = tmp_path / "run.py"
    runner.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            from auto_maintainer.selfupdate.supervise import supervise
            sys.exit(supervise(
                [sys.executable, {str(child)!r}], Path({str(tmp_path)!r}),
                Path({str(tmp_path / "deploy.json")!r}), stop_grace={grace},
            ))
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(runner)], cwd=tmp_path)
    deadline = time.monotonic() + 10
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline, "the service never started"
        time.sleep(0.05)
    return proc


GRACEFUL = """
    import pathlib, signal, sys, time
    def bye(*_):
        pathlib.Path("stopped").write_text("term")
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pathlib.Path("started").write_text("")
    while True:
        time.sleep(0.1)
"""

STUBBORN = """
    import pathlib, signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pathlib.Path("started").write_text(str(__import__("os").getpid()))
    while True:
        time.sleep(0.1)
"""


def test_the_supervisor_stops_its_service_before_it_exits(tmp_path: Path) -> None:
    proc = _run_supervisor(tmp_path, GRACEFUL, grace=10)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 0
    assert (tmp_path / "stopped").read_text() == "term"


def test_a_service_that_will_not_stop_is_killed(tmp_path: Path) -> None:
    proc = _run_supervisor(tmp_path, STUBBORN, grace=0.5)
    pid = int((tmp_path / "started").read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    try:
        import os

        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive
