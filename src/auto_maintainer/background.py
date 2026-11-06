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

"""Running the service in the background: ``am start``, ``am stop``, ``am status``, and
``am logs``.

``am start`` launches ``am supervise --log-file`` in a session of its own, so closing
the terminal does not stop it, and records its process id in ``<data>/am.pid``. The
supervisor writes the service's output to ``<data>/logs/service.log``; what the
supervisor itself prints before it gets that far goes to ``<data>/logs/start.log``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path


def pid_file(data: Path) -> Path:
    return data / "am.pid"


def log_file(data: Path) -> Path:
    return data / "logs" / "service.log"


def start_log(data: Path) -> Path:
    return data / "logs" / "start.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return b"auto_maintainer" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def running(data: Path) -> int | None:
    """The background supervisor's process id, or ``None`` when none runs."""
    try:
        pid = int(pid_file(data).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def start(command: list[str], data: Path, cwd: Path) -> int:
    """Launch ``command`` (an ``am supervise`` command line) in the background.

    Raises:
        RuntimeError: The service already runs in the background.
    """
    if (pid := running(data)) is not None:
        raise RuntimeError(f"the service already runs in the background (pid {pid})")
    path = start_log(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as out:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file(data).write_text(str(proc.pid))
    return proc.pid


def stop(data: Path, timeout: float = 45.0) -> bool:
    """Ask the background supervisor to stop the service and wait for it; ``False``
    when it was not running."""
    pid = running(data)
    if pid is None:
        pid_file(data).unlink(missing_ok=True)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            break
        time.sleep(0.2)
    else:
        os.killpg(pid, signal.SIGKILL)
    pid_file(data).unlink(missing_ok=True)
    return True


def tail(path: Path, lines: int) -> list[str]:
    """The last ``lines`` lines of ``path``; empty when it does not exist."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=max(0, lines))]


def follow(path: Path, poll: float = 0.5) -> Iterator[str]:
    """Lines added to ``path`` from the moment of this call, across rotations, until
    interrupted. The starting point is taken now, not at the first ``next()``."""
    position = path.stat().st_size if path.exists() else 0
    inode = path.stat().st_ino if path.exists() else None
    return _follow(path, poll, position, inode)


def _follow(path: Path, poll: float, position: int, inode: int | None) -> Iterator[str]:
    while True:
        if path.exists():
            stat = path.stat()
            if stat.st_ino != inode or stat.st_size < position:
                inode, position = stat.st_ino, 0
            if stat.st_size > position:
                with path.open(encoding="utf-8", errors="replace") as f:
                    f.seek(position)
                    chunk = f.read()
                    position = f.tell()
                yield from chunk.splitlines()
        time.sleep(poll)
