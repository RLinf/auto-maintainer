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

"""The environment agent subprocesses get, and scrubbing of secrets from their output.

Agent processes see only allowlisted variables from the service's environment.
Forge tokens never pass through from the parent: a task gets one only when the
caller puts a down-scoped token into ``task.env``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping

from ..domain.agents import AgentTask

BASE_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "TERM",
    "TMPDIR",
    "TZ",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)
"""Names (``fnmatch`` patterns) every agent process may inherit."""

NEVER_INHERIT: frozenset[str] = frozenset(
    {"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_PAT"}
)
"""Names that are never inherited from the parent, whatever the allowlist says."""


def build_env(
    task: AgentTask,
    allow_extra: Iterable[str] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for an agent subprocess.

    Args:
        task: The task; its ``env`` is added last and may set any name, including a
            down-scoped forge token.
        allow_extra: More names or ``fnmatch`` patterns to inherit, such as a
            runtime's own API key variable.
        base: The parent environment; ``os.environ`` when ``None``.

    Returns:
        The environment mapping for the subprocess.
    """
    parent = os.environ if base is None else base
    patterns = [*BASE_ALLOWLIST, *allow_extra]
    env = {
        name: value
        for name, value in parent.items()
        if name not in NEVER_INHERIT
        and any(fnmatch.fnmatchcase(name, p) for p in patterns)
    }
    env.update(task.env)
    return env


_REDACTED = "[REDACTED]"
_SCRUB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{16,}"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), _REDACTED),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 " + _REDACTED),
    (
        re.compile(
            r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?!bearer|token)[^\s\"',]{12,}"
        ),
        r"\1" + _REDACTED,
    ),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@\"']+(?::[^/\s@\"']*)?@"),
        r"\1" + _REDACTED + "@",
    ),
)


_KNOWN_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Scrub this exact value from now on, whatever it looks like (a provider's key)."""
    if len(value) >= 8:
        _KNOWN_SECRETS.add(value)


def scrub(text: str) -> str:
    """Return ``text`` with tokens, API keys, and URL credentials replaced by ``[REDACTED]``."""
    for value in _KNOWN_SECRETS:
        if value in text:
            text = text.replace(value, _REDACTED)
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
