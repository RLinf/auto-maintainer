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
- ``providers.json``: their own model APIs (``Provider``, by id), without keys.
- ``keys.json`` (mode 0600): each of those APIs' key, as ``provider:<id>``. Nothing
  reads them back but the runs of their own tasks; the dashboard sees only whether
  one is set.

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
from typing import Any

from pydantic import ValidationError

from ..domain.agents import PersonalAgent
from ..domain.settings import PROVIDER_ID, Provider

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _key_name(provider_id: str) -> str:
    return f"provider:{provider_id}"


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
        folder = self.folder(login)
        await self._migrate(folder)
        out = {}
        for name, value in self._read(folder / "agents.json").items():
            try:
                out[name] = PersonalAgent.model_validate({**value, "agent": name})
            except (ValidationError, TypeError):
                continue
        return out

    async def put(self, login: str, setup: PersonalAgent) -> None:
        folder = self.folder(login)
        await self._migrate(folder)
        async with self._lock:
            path = folder / "agents.json"
            data = self._read(path)
            data[setup.agent] = setup.model_dump(
                mode="json", exclude={"agent"}, exclude_none=True
            )
            _write(path, data)

    async def providers(self, login: str) -> dict[str, Provider]:
        folder = self.folder(login)
        await self._migrate(folder)
        out = {}
        for pid, value in self._read(folder / "providers.json").items():
            try:
                out[pid] = Provider.model_validate(value)
            except (ValidationError, TypeError):
                continue
        return out

    async def put_provider(
        self, login: str, provider_id: str, provider: Provider | None
    ) -> None:
        """Save the person's model API ``provider_id``, or remove it (``None``) with
        its key, and point their agents that used it back at their CLI's sign-in.

        Raises:
            ValueError: ``provider_id`` is not lowercase words joined by hyphens.
        """
        if not PROVIDER_ID.match(provider_id):
            raise ValueError(
                "ids are lowercase words joined by hyphens, such as my-gateway"
            )
        folder = self.folder(login)
        await self._migrate(folder)
        async with self._lock:
            path = folder / "providers.json"
            data = self._read(path)
            if provider is not None:
                data[provider_id] = provider.model_dump(
                    mode="json", exclude={"api_key"}, exclude_none=True
                )
                _write(path, data)
                return
            data.pop(provider_id, None)
            _write(path, data)
            keys = self._read(folder / "keys.json")
            if keys.pop(_key_name(provider_id), None) is not None:
                _write(folder / "keys.json", keys)
            agents = self._read(folder / "agents.json")
            for value in agents.values():
                if isinstance(value, dict) and value.get("provider") == provider_id:
                    for field in ("provider", "model", "effort"):
                        value.pop(field, None)
            _write(folder / "agents.json", agents)

    async def provider_key(self, login: str, provider_id: str) -> str | None:
        folder = self.folder(login)
        await self._migrate(folder)
        value = self._read(folder / "keys.json").get(_key_name(provider_id))
        return str(value) if value else None

    async def set_provider_key(
        self, login: str, provider_id: str, value: str | None
    ) -> None:
        folder = self.folder(login)
        await self._migrate(folder)
        async with self._lock:
            path = folder / "keys.json"
            keys = self._read(path)
            if value:
                keys[_key_name(provider_id)] = value
            else:
                keys.pop(_key_name(provider_id), None)
            _write(path, keys)

    async def keyed(self, login: str) -> set[str]:
        """The ids of the person's model APIs whose key is stored."""
        folder = self.folder(login)
        await self._migrate(folder)
        return {
            name.removeprefix("provider:")
            for name, value in self._read(folder / "keys.json").items()
            if name.startswith("provider:") and value
        }

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

    async def _migrate(self, folder: Path) -> None:
        """Move model APIs kept the earlier way, inline in one agent's setup with the
        key under the agent's name, to model APIs of their own (``<agent>-api``)."""
        if not any(
            isinstance(v, dict) and isinstance(v.get("provider"), dict)
            for v in self._read(folder / "agents.json").values()
        ):
            return
        async with self._lock:
            agents = self._read(folder / "agents.json")
            providers = self._read(folder / "providers.json")
            keys = self._read(folder / "keys.json")
            for name, value in agents.items():
                inline = value.get("provider") if isinstance(value, dict) else None
                if not isinstance(inline, dict):
                    continue
                pid = _free_id(name, providers)
                models = [m for m in inline.get("models") or [] if isinstance(m, str)]
                providers[pid] = {
                    "name": f"{name} API",
                    "protocol": inline.get("protocol"),
                    "base_url": inline.get("base_url"),
                    "models": models,
                }
                value["provider"] = pid
                value.pop("has_key", None)
                if models and not value.get("model"):
                    value["model"] = models[0]
                if keys.get(name):
                    keys[_key_name(pid)] = keys.pop(name)
            _write(folder / "providers.json", providers)
            _write(folder / "keys.json", keys)
            _write(folder / "agents.json", agents)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


def _free_id(agent: str, taken: dict[str, Any]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-") or "agent"
    if not base[0].isalpha():
        base = f"api-{base}"
    pid, n = f"{base}-api", 2
    while pid in taken:
        pid, n = f"{base}-api-{n}", n + 1
    return pid


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
