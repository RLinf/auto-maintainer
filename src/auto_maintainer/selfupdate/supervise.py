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
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

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


def supervise(
    command: list[str],
    checkout: Path,
    state_file: Path,
    *,
    max_backoff: float = 60.0,
    log: Callable[[str], None] = lambda m: print(
        f"[am supervise] {m}", file=sys.stderr
    ),
) -> int:
    backoff = 1.0
    child: subprocess.Popen | None = None

    def stop(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        child = subprocess.Popen(command, cwd=checkout)
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
