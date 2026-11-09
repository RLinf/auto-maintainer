#!/usr/bin/env python3
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

"""``lab``: run a command on the test machine of this reproduction session.

The runtime writes this file, as it is, into an agent run that has a reproduction
session (``AgentTask.lab_session``) and puts it on the agent's ``PATH``. It uses only
the standard library, so any Python 3 in the agent's container runs it. Each command
runs in a fresh shell in the session's folder for one side of the PR (``head`` by
default, or ``base``) on the machine; its output streams here as it comes, and
``lab`` exits with the command's exit code. When the agent changes the code (fixing
CI), ``lab --sync`` copies its working directory to the head side first.

    lab [--side head|base] [--timeout MINUTES] -- COMMAND ...
    lab --sync
    lab --info
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys

END = "\x1e"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=None)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def _usage() -> int:
    print(__doc__.split("\n\n")[-1].strip(), file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    token = os.environ.get("AM_LAB_SESSION", "")
    path = os.environ.get("AM_LAB_SOCKET", "")
    if not token or not path:
        print("lab: this run has no reproduction session", file=sys.stderr)
        return 2
    side, minutes, rest = "head", None, list(argv)
    if rest[:1] == ["--info"]:
        conn = _UnixConnection(path)
        conn.request("GET", "/session", headers={"x-lab-session": token})
        reply = conn.getresponse()
        print(reply.read().decode())
        return 0 if reply.status == 200 else 1
    if rest[:1] == ["--sync"]:
        conn = _UnixConnection(path)
        conn.request(
            "POST",
            "/sync",
            body=json.dumps({"side": "head"}),
            headers={"x-lab-session": token, "content-type": "application/json"},
        )
        reply = conn.getresponse()
        out = json.loads(reply.read().decode() or "{}")
        if reply.status != 200 or out.get("error"):
            print(f"lab: {out.get('error') or reply.status}", file=sys.stderr)
            return 1
        print(
            f"copied {out.get('files', 0)} files to the machine"
            + (f", removed {out['removed']}" if out.get("removed") else "")
        )
        return 0
    while rest and rest[0] != "--":
        flag = rest.pop(0)
        if flag == "--side" and rest:
            side = rest.pop(0)
        elif flag == "--timeout" and rest:
            minutes = rest.pop(0)
        else:
            return _usage()
    if not rest or rest[0] != "--" or len(rest) < 2:
        return _usage()
    command = " ".join(rest[1:]) if len(rest) > 2 else rest[1]
    body = json.dumps({"command": command, "side": side, "timeout_minutes": minutes})
    conn = _UnixConnection(path)
    conn.request(
        "POST",
        "/run",
        body=body,
        headers={"x-lab-session": token, "content-type": "application/json"},
    )
    reply = conn.getresponse()
    if reply.status != 200:
        print(f"lab: {reply.read().decode()}", file=sys.stderr)
        return 1
    outcome: dict = {}
    while True:
        raw = reply.readline()
        if not raw:
            break
        line = raw.decode(errors="replace")
        if line.startswith(END):
            outcome = json.loads(line[1:])
            break
        sys.stdout.write(line)
        sys.stdout.flush()
    if outcome.get("error"):
        print(f"lab: {outcome['error']}", file=sys.stderr)
        return 1
    if outcome.get("refused"):
        print(
            "lab: the machine's rules refused this command: "
            + "; ".join(outcome["refused"]),
            file=sys.stderr,
        )
    if outcome.get("timed_out"):
        print("lab: the command timed out", file=sys.stderr)
    return int(outcome.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
