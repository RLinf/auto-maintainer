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

"""What a reproduction script may not do on a shared test machine (design 5.10).

These patterns catch commands that hurt everyone else on the machine (stopping other
jobs, rebooting, wiping a home directory) before anyone is asked to approve a script.
They do not confine a script: the maintainer who approves it reads it, and a machine
that needs more isolation runs reproductions in a container or under its own user.
"""

from __future__ import annotations

import re

BUILTIN_FORBID: tuple[tuple[str, str], ...] = (
    (r"\bsudo\b", "uses sudo"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "shuts the machine down"),
    (r"\bray\s+stop\b", "stops every Ray job on the machine"),
    (r"\b(pkill|killall)\b|\bkill\s+-\d+\s+-1\b", "kills processes it did not start"),
    (r"\bmkfs\b|\bdd\s+[^\n]*of=/dev/", "writes to a disk device"),
    (r"nvidia-smi\s+[^\n]*(-r\b|--gpu-reset|-pm\b|-pl\b)", "changes GPU settings"),
    (
        r"\brm\s+(-[A-Za-z]+\s+)*(/|~|\$HOME|\$\{HOME\})/?(\s|$|;|&)",
        "removes a root or home directory",
    ),
    (r":\(\)\s*\{", "is a fork bomb"),
    (r"\bcrontab\b|\bsystemctl\b", "changes services or scheduled jobs"),
    (
        r"(>>?\s*|\btee\s+(-a\s+)?)(/etc|/usr|/boot|/var/lib)/",
        "writes under a system directory",
    ),
)


def script_refusals(text: str, extra: list[str] | None = None) -> list[str]:
    """Why ``text`` (setup and script together) may not run; empty when it may."""
    out = []
    for pattern, why in BUILTIN_FORBID:
        if re.search(pattern, text):
            out.append(why)
    for pattern in extra or []:
        try:
            if re.search(pattern, text):
                out.append(f"matches the machine's forbidden pattern {pattern!r}")
        except re.error:
            out.append(f"the machine's pattern {pattern!r} is not valid")
    return out
