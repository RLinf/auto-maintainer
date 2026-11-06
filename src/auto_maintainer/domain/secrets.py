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

"""Patterns for credentials that must never appear in text the service posts.

Agents can read files on the service host, including token files, so anything an
agent writes is checked for these before it becomes public.
"""

from __future__ import annotations

import re

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("API key", re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credentials in a URL", re.compile(r"\bhttps?://[^/\s:@]+:[^/\s@]{8,}@")),
)


def find_secrets(text: str) -> list[str]:
    """Names of the credential kinds found in ``text``."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text or "")]


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """``text`` with every credential replaced by a note, and the kinds found."""
    found = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            found.append(name)
            text = pattern.sub(f"[removed {name}]", text)
    return text, found
