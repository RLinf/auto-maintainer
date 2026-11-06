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

"""``SecretStore`` in a file on the service host, readable only by its owner."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.settings import SECRET_NAME, SecretInfo


class FileSecretStore:
    """Secrets set from the dashboard, in a JSON file with mode 0600, written atomically.

    A name not in the file falls back to the host environment, where ``secrets.env``
    put it at startup.
    """

    def __init__(
        self,
        path: Path,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self.environ = os.environ if environ is None else environ
        self.now = now
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        if self.path.stat().st_mode & 0o077:
            raise PermissionError(f"{self.path} must be readable only by its owner")
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    async def get(self, name: str) -> str | None:
        data = await asyncio.to_thread(self._read)
        entry = data.get(name)
        if entry and entry.get("value"):
            return str(entry["value"])
        return self.environ.get(name) or None

    async def describe(self, names: list[str]) -> list[SecretInfo]:
        data = await asyncio.to_thread(self._read)
        out = []
        for name in sorted(set(names) | set(data)):
            entry = data.get(name)
            if entry and entry.get("value"):
                at = entry.get("updated_at")
                out.append(
                    SecretInfo(
                        name=name,
                        configured=True,
                        source="dashboard",
                        updated_at=datetime.fromisoformat(at) if at else None,
                        updated_by=entry.get("updated_by"),
                    )
                )
            elif self.environ.get(name):
                out.append(SecretInfo(name=name, configured=True, source="host"))
            else:
                out.append(SecretInfo(name=name, configured=False, source="missing"))
        return out

    async def set(self, name: str, value: str, by: str) -> None:
        if not SECRET_NAME.match(name):
            raise ValueError(f"{name!r} is not a secret name such as DEEPSEEK_API_KEY")
        value = value.strip()
        if not value:
            raise ValueError("the value is empty")
        async with self._lock:
            data = await asyncio.to_thread(self._read)
            data[name] = {
                "value": value,
                "updated_at": self.now().isoformat(),
                "updated_by": by,
            }
            await asyncio.to_thread(self._write, data)

    async def delete(self, name: str) -> None:
        async with self._lock:
            data = await asyncio.to_thread(self._read)
            if data.pop(name, None) is not None:
                await asyncio.to_thread(self._write, data)
