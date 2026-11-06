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

"""``am supervise``: keep ``am serve`` running across deploys, and roll back bad ones.

The supervisor starts the service and waits. Exit code ``RESTART_EXIT_CODE`` means a
maintainer deployed new code: start again at once. Any other failure restarts with a
growing delay, except when the service dies before it confirmed a fresh deploy
(``deploy.json`` without ``confirmed``): then the checkout goes back to the previous
commit, its environment is synced, and the service starts on the old code. Exit code
0 (a deliberate stop) ends the supervisor.

With a log file (``am start`` runs it that way), the supervisor writes everything the
service prints, and its own messages, to that file, rotating it by size. It is the
only writer, so the rotation never races another process.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

from .updater import RESTART_EXIT_CODE


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rollback(
    checkout: Path,
    state_file: Path,
    log: Callable[[str], None],
    sync_command: list[str] | None = None,
) -> bool:
    """Return the checkout to the commit before the last deploy."""
    state = read_state(state_file)
    if not state or state.get("confirmed") or not state.get("previous_sha"):
        return False
    previous = str(state["previous_sha"])
    log(f"the new version failed to start; rolling back to {previous[:12]}")
    ok = (
        subprocess.call(["git", "checkout", "-q", "--detach", previous], cwd=checkout)
        == 0
        and subprocess.call(sync_command or ["uv", "sync", "--frozen"], cwd=checkout)
        == 0
    )
    state.update({"confirmed": True, "rolled_back": True, "rolled_back_ok": ok})
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return ok


class LogFile:
    """Lines into ``path``, rotated at ``max_bytes`` with ``backups`` old files kept."""

    def __init__(
        self, path: Path, max_bytes: int = 20_000_000, backups: int = 5
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.Logger(f"am.supervise.{path}")
        self._logger.addHandler(handler)
        self._logger.propagate = False
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        with self._lock:
            self._logger.info(line.rstrip("\n"))

    def pump(self, stream: IO[str]) -> None:
        """Copy ``stream`` line by line until it closes."""
        for line in stream:
            self.write(line)


class _Stop(Exception):
    """Ctrl+C or SIGTERM reached the supervisor."""


def supervise(
    command: list[str],
    checkout: Path,
    state_file: Path,
    *,
    max_backoff: float = 60.0,
    stop_grace: float = 20.0,
    log: Callable[[str], None] | None = None,
    log_file: Path | None = None,
) -> int:
    """Run ``command`` until it exits with 0 or the supervisor is told to stop.

    On Ctrl+C or SIGTERM the service gets one SIGTERM and ``stop_grace`` seconds to
    finish; a second Ctrl+C, or the grace running out, kills it. The supervisor never
    exits while the service is still running.
    """
    sink = LogFile(log_file) if log_file else None
    if log is None:
        if sink is not None:
            to_file = sink

            def log(m: str) -> None:
                stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                to_file.write(f"{stamp} INFO am.supervise: {m}")
        else:

            def log(m: str) -> None:
                print(f"[am supervise] {m}", file=sys.stderr)

    env = {**os.environ, "PYTHONUNBUFFERED": "1"} if sink else None

    def stop(_signum: int, _frame: object) -> None:
        raise _Stop

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        return _run(
            command, checkout, state_file, log, env, sink, max_backoff, stop_grace
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _run(
    command: list[str],
    checkout: Path,
    state_file: Path,
    log: Callable[[str], None],
    env: dict[str, str] | None,
    sink: LogFile | None,
    max_backoff: float,
    stop_grace: float,
) -> int:
    backoff = 1.0
    child: subprocess.Popen | None = None
    try:
        while True:
            if sink is None:
                child = subprocess.Popen(command, cwd=checkout)
            else:
                child = subprocess.Popen(
                    command,
                    cwd=checkout,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                )
                assert child.stdout is not None
                threading.Thread(
                    target=sink.pump, args=(child.stdout,), daemon=True
                ).start()
            code = child.wait()
            if code == 0:
                return 0
            if code == RESTART_EXIT_CODE:
                log("restarting on the deployed code")
                backoff = 1.0
                continue
            if rollback(checkout, state_file, log):
                backoff = 1.0
                continue
            log(f"the service exited with {code}; restarting in {backoff:.0f} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
    except _Stop:
        if child is None or child.poll() is not None:
            return 0
        running = child

        def kill_now(_signum: int, _frame: object) -> None:
            running.kill()

        signal.signal(signal.SIGINT, kill_now)
        signal.signal(signal.SIGTERM, kill_now)
        log("stopping the service (Ctrl+C again to kill it now)")
        # A Ctrl+C in the terminal already reached the service too: one SIGTERM
        # more lets it shut down gracefully, where a forwarded SIGINT would force it.
        running.terminate()
        try:
            running.wait(timeout=stop_grace)
        except subprocess.TimeoutExpired:
            log(f"the service did not stop within {stop_grace:.0f} s; killing it")
            running.kill()
            running.wait()
        return 0
