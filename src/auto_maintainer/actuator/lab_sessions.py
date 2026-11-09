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

"""The reproduction sessions the actuator opened, as playbooks see them (design 5.10).

The ``lab.reproduce`` handler opens a session once its plan is approved; the
reproduce playbook gets the session's token for its agent here, and closes the
session here when the agent is done, which removes its run folder and releases its
GPU on the machine. A session also closes at its time limit.
"""

from __future__ import annotations

from typing import Any

from .context import LabRunner


class LabSessions:
    def __init__(self, labs: LabRunner | None) -> None:
        self._labs = labs

    def token(self, session_id: str) -> str | None:
        return self._labs.session_token(session_id) if self._labs else None

    async def close(self, session_id: str) -> dict[str, Any] | None:
        return await self._labs.close_session(session_id) if self._labs else None
