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

"""Forges and the names of what lives on them (design 5.16).

A repository is named ``<forge>:<path>``, where the path is the forge's own full
project path (``owner/repo`` on GitHub, ``group/subgroup/project`` on GitLab); on the
default forge the prefix is left out, so names from before forges stay as they were.
People are named the same way: a login means something only on its own forge.

``configure`` sets which forge is the default and which exist, once, when the service
starts; until then the default is ``github`` and any forge id is accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_FORGE = "github"

_FORGE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}$")
_LOGIN = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,254})$")
MAX_DEPTH = 21
"""GitLab nests groups up to 20 deep; with the project, 21 segments."""


@dataclass
class _Forges:
    default: str = DEFAULT_FORGE
    known: frozenset[str] = field(default_factory=frozenset)


_forges = _Forges()


def configure(default: str, known: set[str] | frozenset[str]) -> None:
    """Name the default forge and every forge this service serves."""
    if not _FORGE.match(default) or default not in known:
        raise ValueError(f"the default forge {default!r} is not among {sorted(known)}")
    _forges.default = default
    _forges.known = frozenset(known)


def default_forge() -> str:
    return _forges.default


@dataclass(frozen=True)
class RepoRef:
    """A repository: its forge's id and its full path there."""

    forge: str
    path: str

    @property
    def name(self) -> str:
        """The canonical name: the path alone on the default forge."""
        return (
            self.path if self.forge == _forges.default else f"{self.forge}:{self.path}"
        )

    @property
    def namespace(self) -> str:
        """Everything before the last segment: the owner, or the group path."""
        return self.path.rsplit("/", 1)[0]

    @property
    def project(self) -> str:
        """The last segment: the repository's own name."""
        return self.path.rsplit("/", 1)[1]

    def __str__(self) -> str:
        return self.name


def parse_repo(name: str) -> RepoRef:
    """The repository ``name`` names.

    Raises:
        ValueError: ``name`` is not ``[<forge>:]<segment>/<segment>[/...]``, or names
            a forge this service does not serve.
    """
    raw = (name or "").strip()
    forge, sep, path = raw.partition(":")
    if not sep:
        forge, path = _forges.default, raw
    if not _FORGE.match(forge):
        raise ValueError(f"{forge!r} is not a forge id")
    if _forges.known and forge not in _forges.known:
        raise ValueError(f"this service serves no forge {forge!r}")
    segments = path.split("/")
    if not 2 <= len(segments) <= MAX_DEPTH or not all(
        _SEGMENT.match(s) and s not in (".", "..") for s in segments
    ):
        raise ValueError(
            f"expected a repository as 'owner/repo' or 'forge:group/project', got {name!r}"
        )
    return RepoRef(forge=forge, path=path)


def check_repo(name: str) -> str:
    """``name`` in its canonical form, or ``ValueError`` when it names no repository."""
    return parse_repo(name).name


def memory_dir(repo: str) -> str:
    """The repository's folder in the memory repo: ``repos/<owner>/<repo>`` for a
    two-part name on the default forge (as before forges), else
    ``forges/<forge>/<path>/-``. The ``-`` ends the path, as in GitLab's own
    addresses: no project path has a segment ``-``, so a file's repository is never
    in doubt (``repo_of_memory_path``). A name that is no repository, such as the
    service's own ``service``, gets ``repos/<name>``, which holds nothing."""
    try:
        ref = parse_repo(repo)
    except ValueError:
        return f"repos/{repo}"
    if ref.forge == _forges.default and ref.path.count("/") == 1:
        return f"repos/{ref.path}"
    return f"forges/{ref.forge}/{ref.path}/-"


def repo_of_memory_path(path: str) -> str | None:
    """The repository a memory repo file belongs to, if any (``memory_dir``)."""
    parts = path.split("/")
    try:
        if len(parts) >= 3 and parts[0] == "repos":
            return check_repo(f"{parts[1]}/{parts[2]}")
        if len(parts) >= 5 and parts[0] == "forges" and "-" in parts[2:]:
            end = parts.index("-", 2)
            return check_repo(f"{parts[1]}:{'/'.join(parts[2:end])}")
    except ValueError:
        return None
    return None


def fs_parts(repo: str) -> tuple[str, ...]:
    """Folder names for the repository's own files on the host: ``owner``, ``repo``
    on the default forge for a two-part name, else ``@<forge>`` and the path's
    segments; a name that is no repository is one folder. Never parsed back."""
    try:
        ref = parse_repo(repo)
    except ValueError:
        return (repo.replace("/", "__").replace(":", "~") or "_",)
    segments = tuple(ref.path.split("/"))
    if ref.forge == _forges.default and len(segments) == 2:
        return segments
    return (f"@{ref.forge}", *segments)


def slug(repo: str) -> str:
    """One file name for the repository: its canonical name with ``/`` as ``__``
    and the forge's ``:`` as ``~``."""
    return check_repo(repo).replace("/", "__").replace(":", "~")


def forge_of(repo: str) -> str:
    """The id of the forge ``repo`` is on."""
    return parse_repo(repo).forge


def same_forge(a: str, b: str) -> bool:
    return forge_of(a) == forge_of(b)


@dataclass(frozen=True)
class Person:
    """Someone on a forge: its id and their login there."""

    forge: str
    login: str

    @property
    def name(self) -> str:
        """The canonical name: the login alone on the default forge."""
        return (
            self.login
            if self.forge == _forges.default
            else f"{self.forge}:{self.login}"
        )

    def __str__(self) -> str:
        return self.name


def parse_person(name: str) -> Person:
    """The person ``name`` names: ``login`` on the default forge, ``forge:login``
    elsewhere; a leading ``@`` is dropped.

    Raises:
        ValueError: It is no login.
    """
    raw = (name or "").strip().lstrip("@")
    forge, sep, login = raw.partition(":")
    if not sep:
        forge, login = _forges.default, raw
    if not _FORGE.match(forge) or not _LOGIN.match(login):
        raise ValueError(f"{name!r} is not a login")
    if _forges.known and forge not in _forges.known:
        raise ValueError(f"this service serves no forge {forge!r}")
    return Person(forge=forge, login=login)


def check_person(name: str) -> str:
    """``name`` in its canonical form, or ``ValueError`` when it is no login."""
    return parse_person(name).name


def person_on(forge: str, login: str) -> str:
    """The canonical name of ``login`` on ``forge``."""
    return Person(forge=forge, login=login).name


@dataclass(frozen=True)
class ForgeCapabilities:
    """What a forge can do, for playbooks to ask before proposing and for the
    actuator to refuse what it cannot (design 5.16)."""

    draft: bool = True
    """A change can be marked as a draft, and back."""
    requested_reviewers: bool = True
    blocking_verdict: bool = True
    """A review can request changes, which keeps the change from merging."""
    held_ci: bool = True
    """CI of an outside contributor's change waits for a maintainer to approve it."""
    update_branch: str = "merge"
    """How a change catches up with its base: ``merge``, ``rebase``, or ``""``."""
    suggestions: str = "```suggestion"
    """How a review comment opens a suggested change; ``""`` when there are none."""
    review_comments_on_old_lines: bool = True


@dataclass(frozen=True)
class ForgeTerms:
    """The forge's own words for what the service posts and shows."""

    change: str = "pull request"
    change_short: str = "PR"
    change_number_prefix: str = "#"
    forge_name: str = "GitHub"
