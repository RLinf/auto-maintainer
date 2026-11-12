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
- ``machines.json``: their own test machines (``LabMachine``, by name), which only
  the work they ask for runs on.
- ``ssh/``: the SSH key the service made for them (they add its public half to their
  machines and their jump hosts; the private half never leaves this folder, and no
  agent's container mounts it), their machines' known hosts, and the SSH config the
  service reaches their machines with: that key alone, no agent, none of the
  service's own config. OpenSSH hands the same config (``-F``) to the ssh it starts
  for each jump host, so jump hosts are reached the same way.

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
from ..domain.labs import personal_name
from ..domain.settings import PROVIDER_ID, LabMachine, Provider
from ..subproc import run

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

    def ssh_dir(self, login: str) -> Path:
        path = self.folder(login) / "ssh"
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
        return path

    async def ssh_public_key(self, login: str) -> str:
        """The person's own SSH public key, made the first time it is asked for; the
        private key stays in their folder on the host.

        Raises:
            RuntimeError: ``ssh-keygen`` could not make it.
        """
        folder = self.ssh_dir(login)
        key = folder / "id_ed25519"
        async with self._lock:
            if not key.is_file():
                made = await run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-C",
                        f"auto-maintainer for {login.lower()}",
                        "-f",
                        str(key),
                    ],
                    timeout=60,
                )
                if made.exit_code != 0 or not key.is_file():
                    raise RuntimeError(
                        f"could not make an SSH key: {made.stderr.strip()[:300]}"
                    )
            key.chmod(0o600)
            self._ssh_config(login)
        return (folder / "id_ed25519.pub").read_text(encoding="utf-8").strip()

    def _ssh_config(self, login: str) -> Path:
        """The SSH config for the person's machines and their jump hosts: their key
        alone, their own known hosts, no SSH agent, nothing forwarded, never a
        prompt. Command-line options reach only the machine itself, so what a jump
        host must obey is here."""
        folder = self.ssh_dir(login)
        path = folder / "config"
        text = "\n".join(
            [
                "Host *",
                f"  IdentityFile {folder / 'id_ed25519'}",
                "  IdentitiesOnly yes",
                "  IdentityAgent none",
                "  ForwardAgent no",
                "  ForwardX11 no",
                f"  UserKnownHostsFile {folder / 'known_hosts'}",
                "  StrictHostKeyChecking accept-new",
                "  PasswordAuthentication no",
                "  KbdInteractiveAuthentication no",
                "  BatchMode yes",
                "  ConnectTimeout 30",
                "",
            ]
        )
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        return path

    async def machines(self, login: str) -> dict[str, LabMachine]:
        """The person's own test machines by name, each with them as its owner and
        the SSH key and config the service reaches it with."""
        folder = self.folder(login)
        out = {}
        for name, value in self._read(folder / "machines.json").items():
            try:
                machine = LabMachine.model_validate(value)
            except (ValidationError, TypeError):
                continue
            ssh = self.ssh_dir(login)
            out[name] = machine.model_copy(
                update={
                    "owner": login.lower(),
                    "identity_file": str(ssh / "id_ed25519"),
                    "ssh_config": str(self._ssh_config(login)),
                }
            )
        return out

    async def put_machine(
        self, login: str, name: str, machine: LabMachine | None
    ) -> None:
        """Save the person's machine ``name`` as they gave it, or remove it (``None``).
        The owner, key, and config are the service's to add when it is read."""
        folder = self.folder(login)
        async with self._lock:
            path = folder / "machines.json"
            data = self._read(path)
            if machine is None:
                data.pop(name, None)
            else:
                data[name] = machine.model_dump(
                    mode="json",
                    exclude={"owner", "ssh_config", "identity_file"},
                    exclude_none=True,
                )
            _write(path, data)

    async def all_machines(self) -> dict[str, LabMachine]:
        """Everyone's own test machines, by their names in the service
        (``login/name``)."""
        out: dict[str, LabMachine] = {}
        if not self.root.is_dir():
            return out
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or not (folder / "machines.json").is_file():
                continue
            try:
                machines = await self.machines(folder.name)
            except ValueError:
                continue
            for name, machine in machines.items():
                out[personal_name(folder.name, name)] = machine
        return out

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
