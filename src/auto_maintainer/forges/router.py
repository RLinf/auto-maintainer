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

"""One ``ForgeReader``, ``ForgeWriter``, or ``CiProvider`` over every forge the
service serves (design 5.16).

Each port method takes the repository first; ``ForgeRouter`` hands the call to the
adapter of the forge that repository is on. Adapters take the canonical name
(``domain/forges.py``) and put only the forge's own path on the wire
(``parse_repo(repo).path``). Services and playbooks keep calling one reader and one
writer, whichever forges there are.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..domain.forges import default_forge, parse_repo


class ForgeRouter:
    """Implements a forge port by delegating to the adapter of each repository's
    forge.

    Args:
        parts: Each forge's adapter for this port, by forge id.
    """

    def __init__(self, parts: Mapping[str, Any]) -> None:
        self.parts = dict(parts)

    def at(self, forge: str) -> Any:
        try:
            return self.parts[forge]
        except KeyError:
            raise ValueError(f"this service serves no forge {forge!r}") from None

    def for_repo(self, repo: str) -> Any:
        return self.at(parse_repo(repo).forge)

    async def service_login(self) -> str:
        """The login the service acts as on the default forge."""
        return await self.at(default_forge()).service_login()

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(repo: str, *args: Any, **kwargs: Any) -> Any:
            return await getattr(self.for_repo(repo), name)(repo, *args, **kwargs)

        call.__name__ = name
        return call
