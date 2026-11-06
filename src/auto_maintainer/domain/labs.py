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

from .profile import LabBinding
from .settings import LabMachine

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


def unusable(
    repo: str, name: str, machine: LabMachine | None, binding: LabBinding | None
) -> str | None:
    """Why ``repo`` cannot reproduce on machine ``name``, or ``None`` when it can.

    The service settings offer a machine (to every repository, or to those it names);
    a repository uses it only when its own profile lists it with ``use: true``.
    """
    if machine is None:
        return f"there is no test machine named {name!r}"
    if not machine.serves(repo):
        return f"test machine {name!r} is not offered to {repo}"
    if binding is None or not binding.use:
        return f"{repo} does not use test machine {name!r}; turn it on in its settings"
    return None


def setup_lines(machine: LabMachine, binding: LabBinding | None) -> list[str]:
    """What runs before a reproduction's own setup: the machine's, then the
    repository's."""
    return [x for x in (machine.setup, binding.setup if binding else None) if x]


def forbidden(machine: LabMachine, binding: LabBinding | None) -> list[str]:
    """Patterns a script must not match beyond the built-in ones: the machine's, the
    repository's, and the repository's protected paths."""
    out = list(machine.forbid)
    if binding is not None:
        out += binding.guard.forbid
        out += [re.escape(p) for p in binding.protected_paths if p]
    return out


def time_limit(machine: LabMachine, binding: LabBinding | None) -> int:
    """The longest a reproduction may run: the machine's limit, or the repository's
    when it is shorter."""
    if binding is not None and binding.timeout_seconds:
        return min(machine.timeout_seconds, binding.timeout_seconds)
    return machine.timeout_seconds
