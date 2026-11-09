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

"""Each person's own agent accounts, on the service's host only.

``<data>/people/<login>/`` (mode 0700) holds, for one signed-in person:

- ``home/``: the home folder their agent CLIs sign in to, holding each CLI's own
  sign-in (``.claude``, ``.codex``, ``.gemini``, ...). Only their own tasks' containers
  mount it, and only what the CLI needs of it.
- ``agents.json``: how they run each agent (``PersonalAgent``), without keys.
- ``keys.json`` (mode 0600): the keys of their own model APIs. Nothing reads them back
  but the runs of their own tasks; the dashboard sees only whether one is set.

Nobody else reaches these through the dashboard: every route works on the signed-in
person's own login.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from ..domain.agents import PersonalAgent

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class PeopleAgents:
    """Implements ``PersonalAgents`` over ``<data>/people``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = asyncio.Lock()

    def folder(self, login: str) -> Path:
        """The person's own folder, made private on first use.

        Raises:
            ValueError: ``login`` is not a GitHub login.
        """
        if not _LOGIN.match(login):
            raise ValueError(f"{login!r} is not a GitHub login")
        folder = self.root / login.lower()
        for path in (self.root, folder):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        return folder

    def home(self, login: str) -> Path:
        home = self.folder(login) / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        return home

    async def get(self, login: str) -> dict[str, PersonalAgent]:
        data = self._read(self.folder(login) / "agents.json")
        keys = self._read(self.folder(login) / "keys.json")
        out = {}
        for name, value in data.items():
            setup = PersonalAgent.model_validate({**value, "agent": name})
            out[name] = setup.model_copy(update={"has_key": bool(keys.get(name))})
        return out

    async def put(self, login: str, setup: PersonalAgent) -> None:
        async with self._lock:
            path = self.folder(login) / "agents.json"
            data = self._read(path)
            data[setup.agent] = setup.model_dump(
                mode="json", exclude={"agent", "has_key"}
            )
            _write(path, data)

    async def key(self, login: str, agent: str) -> str | None:
        value = self._read(self.folder(login) / "keys.json").get(agent)
        return str(value) if value else None

    async def set_key(self, login: str, agent: str, value: str | None) -> None:
        async with self._lock:
            path = self.folder(login) / "keys.json"
            keys = self._read(path)
            if value:
                keys[agent] = value
            else:
                keys.pop(agent, None)
            _write(path, keys)

    async def sign_out(self, login: str, paths: tuple[str, ...]) -> None:
        """Remove one CLI's sign-in (its ``home_paths``) from the person's home."""
        home = self.home(login)
        for rel in paths:
            target = (home / rel).resolve()
            if not target.is_relative_to(home.resolve()) or target == home.resolve():
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
