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

"""``am-read``: read the dashboard's records as the person you are talking with.

The runtime writes this file, as it is, into an agent run that may read the service's
records (``AgentTask.records_token``) and puts it on the agent's ``PATH``. It uses
only the standard library. Each call prints what one path of the dashboard's API
answers that person (JSON, as the dashboard's pages get it); nothing it does changes
anything.

    am-read /api/PATH[?QUERY]   such as /api/prs?mine=true or /api/prs/OWNER/REPO/42
    am-read --index             every path it can read, with what each returns
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
from urllib.parse import quote


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=180)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def _get(path: str) -> tuple[int, str]:
    token = os.environ.get("AM_RECORDS_TOKEN", "")
    where = os.environ.get("AM_RECORDS_SOCKET", "")
    if not token or not where:
        return 2, "am-read works only in a conversation the service started"
    conn = _UnixConnection(where)
    conn.request(
        "GET", "/read?path=" + quote(path, safe=""), headers={"x-records-token": token}
    )
    r = conn.getresponse()
    return r.status, r.read().decode("utf-8", "replace")


def _index() -> int:
    status, text = _get("/api/openapi.json")
    if status != 200:
        print(text, file=sys.stderr)
        return 1
    spec = json.loads(text)
    for path, ops in sorted(spec.get("paths", {}).items()):
        op = ops.get("get")
        if not op or not path.startswith("/api/") or path == "/api/events":
            continue
        params = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
        about = (op.get("description") or op.get("summary") or "").strip()
        about = about.splitlines()[0] if about else ""
        query = f" ?{'&'.join(params)}" if params else ""
        print(f"{path}{query}" + (f"  — {about}" if about else ""))
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--index"]:
        return _index()
    if len(argv) != 1 or not argv[0].startswith("/api/"):
        print(__doc__.split("\n\n")[-1].strip(), file=sys.stderr)
        return 2
    status, text = _get(argv[0])
    try:
        text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
    except ValueError:
        pass
    print(text, file=sys.stdout if status < 400 else sys.stderr)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
