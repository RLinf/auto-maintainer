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

"""Fixtures for agent runtime tests: a fake CLI that replays captured output."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = r"""#!{python}
import json, os, signal, sys, time
cfg = json.loads({cfg!r})
out = cfg["record_dir"]
with open(os.path.join(out, "argv.json"), "w") as f:
    json.dump(sys.argv[1:], f)
with open(os.path.join(out, "env.json"), "w") as f:
    json.dump(dict(os.environ), f)
with open(os.path.join(out, "cwd.txt"), "w") as f:
    f.write(os.getcwd())
if cfg.get("read_stdin"):
    data = sys.stdin.read()
    with open(os.path.join(out, "stdin.txt"), "w") as f:
        f.write(data)
if cfg.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if cfg.get("last_message") is not None and "-o" in sys.argv:
    with open(sys.argv[sys.argv.index("-o") + 1], "w") as f:
        f.write(cfg["last_message"])
sys.stdout.write(cfg.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(cfg.get("stderr", ""))
sys.stderr.flush()
time.sleep(cfg.get("sleep", 0))
sys.exit(cfg.get("exit", 0))
"""


@dataclass
class FakeCli:
    """A fake agent CLI and the directory where it records what it received."""

    path: Path
    record_dir: Path

    def argv(self) -> list[str]:
        return json.loads((self.record_dir / "argv.json").read_text())

    def env(self) -> dict[str, str]:
        return json.loads((self.record_dir / "env.json").read_text())

    def stdin(self) -> str:
        return (self.record_dir / "stdin.txt").read_text()


@pytest.fixture
def fake_cli(tmp_path: Path) -> Callable[..., FakeCli]:
    """Build a fake CLI: ``fake_cli(stdout=..., stderr=..., exit=..., sleep=..., last_message=...)``."""
    counter = {"n": 0}

    def make(**cfg: Any) -> FakeCli:
        counter["n"] += 1
        record = tmp_path / f"fake{counter['n']}"
        record.mkdir()
        cfg.setdefault("read_stdin", True)
        cfg["record_dir"] = str(record)
        script = tmp_path / f"fake-cli-{counter['n']}"
        script.write_text(_SCRIPT.format(python=sys.executable, cfg=json.dumps(cfg)))
        script.chmod(0o755)
        return FakeCli(path=script, record_dir=record)

    return make


_TALKER = r"""#!{python}
import json, os, sys, time
cfg = json.loads({cfg!r})
out = cfg["record_dir"]
with open(os.path.join(out, "argv.json"), "w") as f:
    json.dump(sys.argv[1:], f)
with open(os.path.join(out, "env.json"), "w") as f:
    json.dump(dict(os.environ), f)
received = open(os.path.join(out, "stdin.jsonl"), "w")
for step in cfg["steps"]:
    wait = step.get("wait")
    while wait:
        line = sys.stdin.readline()
        if not line:
            sys.exit(3)
        received.write(line)
        received.flush()
        if wait in line:
            break
    time.sleep(step.get("sleep", 0))
    for emitted in step.get("emit", []):
        sys.stdout.write((emitted if isinstance(emitted, str) else json.dumps(emitted)) + "\n")
        sys.stdout.flush()
    if step.get("die"):
        received.close()
        os._exit(137)
for line in sys.stdin:
    received.write(line)
received.close()
sys.exit(cfg.get("exit", 0))
"""


@dataclass
class TalkingCli:
    """A fake interactive CLI and what it received on standard input."""

    path: Path
    record_dir: Path

    def argv(self) -> list[str]:
        return json.loads((self.record_dir / "argv.json").read_text())

    def env(self) -> dict[str, str]:
        return json.loads((self.record_dir / "env.json").read_text())

    def received(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (self.record_dir / "stdin.jsonl").read_text().splitlines()
            if line.strip()
        ]


@pytest.fixture
def talking_cli(tmp_path: Path) -> Callable[..., TalkingCli]:
    """Build a fake CLI that follows ``steps``: each waits for a standard-input line
    containing ``wait`` (if given), sleeps ``sleep`` seconds (if given), then prints
    ``emit`` (objects become JSON lines); with ``die`` it then ends at once, as a
    killed container does."""
    counter = {"n": 0}

    def make(steps: list[dict[str, Any]], exit: int = 0) -> TalkingCli:
        counter["n"] += 1
        record = tmp_path / f"talker{counter['n']}"
        record.mkdir()
        cfg = {"steps": steps, "exit": exit, "record_dir": str(record)}
        script = tmp_path / f"talking-cli-{counter['n']}"
        script.write_text(_TALKER.format(python=sys.executable, cfg=json.dumps(cfg)))
        script.chmod(0o755)
        return TalkingCli(path=script, record_dir=record)

    return make
