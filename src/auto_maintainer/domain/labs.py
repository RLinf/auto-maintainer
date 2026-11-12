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
from dataclasses import dataclass

from .profile import LabBinding
from .settings import LabMachine

_RUNS = r"(?:^|[;&|(`{]|\$\(|\b(?:then|do|else|exec|nohup|xargs|time|sudo)\b)\s*"
"""Where a word runs as a command: the start of a line or of a command after a
separator, so the same word inside a search pattern or a string does not count."""

BUILTIN_FORBID: tuple[tuple[str, str], ...] = (
    (_RUNS + r"sudo\b", "uses sudo"),
    (_RUNS + r"(shutdown|reboot|poweroff|halt)\b", "shuts the machine down"),
    (_RUNS + r"ray\s+stop\b", "stops every Ray job on the machine"),
    (
        _RUNS + r"(pkill|killall)\b|" + _RUNS + r"kill\s+-\d+\s+-1\b",
        "kills processes it did not start",
    ),
    (_RUNS + r"mkfs\b|" + _RUNS + r"dd\s+[^\n;&|]*of=/dev/", "writes to a disk device"),
    (
        _RUNS + r"nvidia-smi\s+[^\n;&|]*(-r\b|--gpu-reset|-pm\b|-pl\b)",
        "changes GPU settings",
    ),
    (
        _RUNS + r"rm\s+(-[A-Za-z]+\s+)*(/|~|\$HOME|\$\{HOME\})/?(\s|$|;|&)",
        "removes a root or home directory",
    ),
    (r":\(\)\s*\{", "is a fork bomb"),
    (_RUNS + r"(crontab|systemctl)\b", "changes services or scheduled jobs"),
    (
        r"(>>?\s*|\btee\s+(-a\s+)?)(/etc|/usr|/boot|/var/lib)/",
        "writes under a system directory",
    ),
)
"""What no command on a shared test machine may do, matched where the command
runs; a machine and a repository add their own patterns (``forbidden``)."""


def script_refusals(text: str, extra: list[str] | None = None) -> list[str]:
    """Why ``text`` (setup and script together) may not run; empty when it may."""
    out = []
    for pattern, why in BUILTIN_FORBID:
        if re.search(pattern, text, re.MULTILINE):
            out.append(why)
    for pattern in extra or []:
        try:
            if re.search(pattern, text):
                out.append(f"matches the machine's forbidden pattern {pattern!r}")
        except re.error:
            out.append(f"the machine's pattern {pattern!r} is not valid")
    return out


FIX_CI_SESSION = "fix-ci:"
"""How a ``lab.reproduce`` that only holds a machine for fixing CI starts its
discriminator: it is no reproduction of the PR."""


PERSONAL = "/"
"""What joins a person's login and their machine's name: ``<login>/<name>``."""


def personal_name(login: str, name: str) -> str:
    """The name a person's own machine goes by in the service."""
    return f"{login.lower()}{PERSONAL}{name}"


def owns(machine: LabMachine | None, login: str | None) -> bool:
    """Whether ``machine`` is ``login``'s own."""
    return bool(
        machine is not None
        and machine.owner
        and login
        and machine.owner.lower() == login.lower()
    )


def offered(machines: dict[str, LabMachine], repo: str, login: str | None) -> list[str]:
    """The machines ``login`` may see for ``repo``: their own first, then those the
    service settings offer it; never someone else's own."""
    mine = sorted(n for n, m in machines.items() if owns(m, login) and m.serves(repo))
    shared = sorted(n for n, m in machines.items() if not m.owner and m.serves(repo))
    return mine + shared


def first_usable(
    machines: dict[str, LabMachine],
    bindings: dict[str, LabBinding],
    repo: str,
    login: str | None,
) -> str | None:
    """The machine the work ``login`` asks for on ``repo`` runs on: their own first,
    else the first the repository uses; the repository's only for work nobody asked
    for."""
    for name in offered(machines, repo, login):
        if unusable(repo, name, machines.get(name), bindings.get(name), login) is None:
            return name
    return None


def unusable(
    repo: str,
    name: str,
    machine: LabMachine | None,
    binding: LabBinding | None,
    by: str | None = None,
) -> str | None:
    """Why ``repo`` cannot reproduce on machine ``name`` for ``by``, or ``None`` when
    it can.

    The service settings offer a machine (to every repository, or to those it names);
    a repository uses it only when its own profile lists it with ``use: true``. A
    person's own machine (``LabMachine.owner``) runs only the work they ask for, on
    any repository, whatever the repository's profile lists.
    """
    if machine is None:
        return f"there is no test machine named {name!r}"
    if machine.owner:
        if not owns(machine, by):
            return f"test machine {name!r} is {machine.owner}'s own"
        return None
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


@dataclass(frozen=True)
class LabProgress:
    """What a run on a test machine is doing, for people watching it: a new
    ``phase``, or a ``line`` of the output of one ``side`` (``base`` or ``head``)."""

    side: str | None = None
    phase: str = ""
    line: str = ""
