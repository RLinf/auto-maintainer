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

"""The poller (design 5.3): the source of truth for forge state.

It compares fresh snapshots with stored ones and emits an event for each
difference, so a missed webhook costs latency, not correctness.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from ..domain.clock import Clock
from ..domain.codeowners import CODEOWNERS_LOCATIONS
from ..domain.events import Event, EventKind, parse_commands
from ..domain.markers import parse_markers
from ..domain.ports import EventSink, ForgeReader, Store
from ..domain.records import PrAssignee
from ..domain.snapshot import CheckStatus, PrSnapshot, PrState
from ..profiles.loader import IN_REPO_PATH
from ..profiles.service import ProfileService

log = logging.getLogger(__name__)

WATCHED_FILES = (
    IN_REPO_PATH,
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    *CODEOWNERS_LOCATIONS,
)


def diff_snapshots(
    old: PrSnapshot | None, new: PrSnapshot, service_login: str, prefix: str
) -> list[Event]:
    """Events that turn ``old`` into ``new``."""
    base = {
        "repo": new.repo,
        "number": new.number,
        "sha": new.head_sha,
        "source": "poller",
    }
    if old is None or old.state != PrState.OPEN:
        if new.state == PrState.OPEN:
            return [Event(kind=EventKind.PR_OPENED, **base)]
        return []
    events: list[Event] = []
    if new.state != PrState.OPEN:
        return [
            Event(
                kind=EventKind.PR_CLOSED,
                payload={"merged": new.state == PrState.MERGED},
                **base,
            )
        ]
    if new.head_sha != old.head_sha:
        events.append(
            Event(
                kind=EventKind.PR_SYNCHRONIZED, payload={"before": old.head_sha}, **base
            )
        )
    if (new.title, new.body, new.is_draft) != (old.title, old.body, old.is_draft):
        events.append(
            Event(
                kind=EventKind.PR_UPDATED,
                payload={"draft_changed": new.is_draft != old.is_draft},
                **base,
            )
        )
    added = sorted(set(new.labels) - set(old.labels))
    removed = sorted(set(old.labels) - set(new.labels))
    if added or removed:
        events.append(
            Event(
                kind=EventKind.PR_LABELED,
                payload={"added": added, "removed": removed},
                **base,
            )
        )
    seen_reviews = {r.id for r in old.reviews}
    for review in new.reviews:
        if (
            review.id not in seen_reviews
            and review.author.lower() != service_login.lower()
        ):
            events.append(
                Event(
                    kind=EventKind.PR_REVIEW_SUBMITTED,
                    payload={
                        "review_id": review.id,
                        "author": review.author,
                        "state": review.state,
                    },
                    **base,
                )
            )
    seen_comments = {c.id for c in old.comments}
    for comment in new.comments:
        if (
            comment.id in seen_comments
            or comment.author.lower() == service_login.lower()
        ):
            continue
        if any(m.kind for m in parse_markers(comment.body)):
            continue
        commands = [c.model_dump() for c in parse_commands(comment.body, prefix)]
        events.append(
            Event(
                kind=EventKind.PR_COMMENT,
                payload={
                    "comment_id": comment.id,
                    "author": comment.author,
                    "commands": commands,
                },
                **base,
            )
        )
    before = {c.id: c for c in old.checks} if old.head_sha == new.head_sha else {}
    completed = [
        c
        for c in new.checks
        if c.status == CheckStatus.COMPLETED
        and (
            c.id not in before
            or before[c.id].status != CheckStatus.COMPLETED
            or before[c.id].conclusion != c.conclusion
        )
    ]
    if completed:
        events.append(
            Event(
                kind=EventKind.CHECKS_COMPLETED,
                payload={
                    "completed": [c.id for c in completed],
                    "failed": [c.id for c in completed if c.failed],
                },
                **base,
            )
        )
    return events


FORGE_ROLES = "forge_roles:{repo}#{number}"
"""The roles last copied from the forge for a PR (``sync_roles``), as ``role:login``."""


def forge_name(pr: PrSnapshot) -> str:
    """What the dashboard calls the forge ``pr`` is on."""
    return "GitLab" if "/-/merge_requests/" in pr.url else "GitHub"


async def sync_roles(store: Store, pr: PrSnapshot) -> bool:
    """Copy the roles people gave on the forge itself into ``pr``'s roles here: a
    review request a person made (``requested_directly``, not one the forge made for
    a code owner) makes a reviewer, an assignee an owner; the PR's author takes
    neither. Only what changed on the forge since the last copy is copied, so a role
    someone removed here is not given back while the forge still lists it. A role
    from the forge goes when the forge withdraws it, unless the person reviewed (a
    review ends GitHub's request; their role stays). Roles given here are never
    touched. Returns whether any role changed."""
    author = pr.author.lower()
    wanted = {
        f"{role}:{login.lower()}": (role, login)
        for role, logins in (
            ("reviewer", pr.requested_directly),
            ("owner", pr.assignees),
        )
        for login in logins
        if login.lower() != author
    }
    key = FORGE_ROLES.format(repo=pr.repo, number=pr.number)
    before = set(await store.get_state(key) or [])
    if before == set(wanted):
        return False
    held = {
        f"{a.role}:{a.login.lower()}": a
        for a in await store.list_assignees(pr.repo, pr.number)
    }
    reviewed = {r.author.lower() for r in pr.reviews} | {
        c.author.lower() for t in pr.threads for c in t.comments
    }
    changed = False
    for entry in set(wanted) - before:
        if entry in held:
            continue
        role, login = wanted[entry]
        await store.add_assignee(
            PrAssignee(
                repo=pr.repo,
                number=pr.number,
                login=login,
                role=role,  # type: ignore[arg-type]
                kind="forge",
                by=forge_name(pr),
            )
        )
        changed = True
    for entry in before - set(wanted):
        role, login = entry.split(":", 1)
        a = held.get(entry)
        if a is None or a.kind != "forge":
            continue
        if role == "reviewer" and login in reviewed:
            continue
        await store.remove_assignee(pr.repo, pr.number, a.login, role)
        changed = True
    await store.set_state(key, sorted(wanted))
    return changed


class Poller:
    def __init__(
        self,
        forge: ForgeReader,
        store: Store,
        bus: EventSink,
        profiles: ProfileService,
        clock: Clock,
        service_login: str,
        serves: Callable[[str], bool] | None = None,
    ) -> None:
        self.serves = serves or (lambda repo: True)
        self.forge = forge
        self.store = store
        self.bus = bus
        self.profiles = profiles
        self.clock = clock
        self.service_login = service_login

    async def poll_repo(self, repo: str, reconcile: bool = False) -> list[Event]:
        """Poll one repository; with ``reconcile``, also emit ``pr.reconciled`` for every open PR."""
        profile = await self.profiles.active(repo)
        prefix = profile.comments.command_prefix if profile else "/am"
        fresh = {p.number: p for p in await self.forge.list_open_prs(repo)}
        stored = {
            p.number: p for p in await self.store.list_snapshots(repo, open_only=True)
        }
        events: list[Event] = []
        saved = False
        for number, pr in fresh.items():
            old = stored.get(number) or await self.store.get_snapshot(repo, number)
            if old is None or old.fingerprint() != pr.fingerprint():
                events += diff_snapshots(old, pr, self.service_login, prefix)
                await self.store.save_snapshot(pr)
                saved = True
            elif old != pr:
                # Nothing that makes an event changed (a commit's time, a file's
                # counts): keep the fresher record without announcing a change.
                await self.store.save_snapshot(pr)
                saved = True
            if await sync_roles(self.store, pr):
                saved = True
            if reconcile:
                events.append(
                    Event(
                        kind=EventKind.PR_RECONCILED,
                        repo=repo,
                        number=number,
                        sha=pr.head_sha,
                        source="poller",
                    )
                )
        for number, old in stored.items():
            if number in fresh:
                continue
            closed = await self.forge.get_pr(repo, number)
            if closed is None:
                closed = old.model_copy(update={"state": PrState.CLOSED})
            events += diff_snapshots(old, closed, self.service_login, prefix)
            await self.store.save_snapshot(closed)
        record = await self.store.get_repo(repo)
        if record is not None:
            record.last_poll_at = self.clock.now()
            if reconcile:
                record.last_reconcile_at = self.clock.now()
                events += await self._watch_files(repo, record.watched_files)
                record.watched_files = await self._file_hashes(repo)
            await self.store.save_repo(record)
        for event in events:
            await self.bus.publish(event)
        notify = getattr(self.bus, "notify", None)
        if saved and callable(notify):
            # Dashboards show more than what starts playbooks (a resolved thread, a
            # review asked for): one notice per poll that the repository's PRs changed.
            notify(Event(kind=EventKind.PR_REFRESHED, repo=repo, source="poller"))
        return events

    async def poll_pr(self, repo: str, number: int) -> list[Event]:
        """Refresh one PR's snapshot now, as the next poll would, and publish what
        changed; used after the service wrote to it, so the dashboard shows the write
        without waiting for the poll."""
        profile = await self.profiles.active(repo)
        prefix = profile.comments.command_prefix if profile else "/am"
        fresh = await self.forge.get_pr(repo, number)
        if fresh is None:
            return []
        old = await self.store.get_snapshot(repo, number)
        await sync_roles(self.store, fresh)
        if old is not None and old.fingerprint() == fresh.fingerprint():
            if old != fresh:
                await self.store.save_snapshot(fresh)
            return []
        events = diff_snapshots(old, fresh, self.service_login, prefix)
        await self.store.save_snapshot(fresh)
        for event in events:
            await self.bus.publish(event)
        return events

    async def _file_hashes(self, repo: str) -> dict[str, str]:
        hashes = {}
        for path in WATCHED_FILES:
            text = await self.forge.get_file(repo, path)
            hashes[path] = (
                hashlib.sha256(text.encode()).hexdigest()[:16]
                if text is not None
                else ""
            )
        return hashes

    async def _watch_files(self, repo: str, previous: dict[str, str]) -> list[Event]:
        if not previous:
            return []
        current = await self._file_hashes(repo)
        changed = sorted(p for p in current if current[p] != previous.get(p, ""))
        if not changed:
            return []
        return [
            Event(
                kind=EventKind.REPO_FILES_CHANGED,
                repo=repo,
                payload={"paths": changed},
                source="poller",
            )
        ]

    async def poll_all(self, reconcile: bool = False) -> int:
        count = 0
        for record in await self.store.list_repos():
            if not record.active or not self.serves(record.repo):
                continue
            try:
                count += len(await self.poll_repo(record.repo, reconcile))
            except Exception:
                log.exception("polling %s failed", record.repo)
        return count
