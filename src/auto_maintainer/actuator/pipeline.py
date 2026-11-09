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

"""The action pipeline (design 4.3): policy decides, then shadow, propose, or execute.

Every action takes this path, whoever created it. An approved proposal is decided
again before it runs, so a pause, a hard limit, or a moved head still applies.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import timedelta
from typing import Any

from ..domain.actions import (
    PAYLOADS,
    PUBLIC_TEXT_FIELDS,
    Action,
    ActionKind,
    ActionStatus,
    Decision,
    DecisionKind,
    Proposal,
    ProposalState,
)
from ..domain.clock import Clock
from ..domain.events import Event, EventKind
from ..domain.ids import same_commit
from ..domain.memory import Signal, SignalKind
from ..domain.ports import EventSink, Store
from ..domain.profile import RepoProfile
from ..domain.records import ActionRecord, AuditEntry
from ..policy.engine import PAUSED_RULE, WRITE_CAP_RULE, PolicyFacts, decide
from ..policy.leak_guard import TIER_BLIND, check_text
from ..profiles.knowledge import Knowledge
from ..profiles.service import ProfileService
from .executor import Actuator

log = logging.getLogger(__name__)

PAUSE_KEY = "paused"


class ApprovalError(ValueError):
    pass


class ActionPipeline:
    def __init__(
        self,
        store: Store,
        profiles: ProfileService,
        knowledge: Knowledge,
        actuator: Actuator,
        bus: EventSink,
        clock: Clock,
        dev_mode: bool = False,
        write_caps: tuple[int, int] = (120, 40),
        sandbox_repo: str | None = None,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.knowledge = knowledge
        self.actuator = actuator
        self.bus = bus
        self.clock = clock
        self.dev_mode = dev_mode
        self.write_caps = write_caps
        self.sandbox_repo = sandbox_repo

    def capped(self, repo: str) -> bool:
        """Whether dev mode holds ``repo``'s actions at shadow. A development instance
        with a sandbox repository acts on it for real and on nothing else."""
        return self.dev_mode and repo != self.sandbox_repo

    def outside_sandbox(self, repo: str) -> bool:
        return self.dev_mode and bool(self.sandbox_repo) and repo != self.sandbox_repo

    # -- pause ---------------------------------------------------------------------

    async def paused(self, repo: str | None) -> bool:
        if await self.store.get_state(PAUSE_KEY):
            return True
        if repo:
            record = await self.store.get_repo(repo)
            return bool(record and record.paused)
        return False

    async def set_paused(
        self, paused: bool, actor: str, repo: str | None = None, reason: str = ""
    ) -> None:
        if repo is None:
            await self.store.set_state(PAUSE_KEY, paused)
        else:
            record = await self.store.get_repo(repo)
            if record is None:
                raise ValueError(f"{repo} is not configured")
            record.paused = paused
            await self.store.save_repo(record)
        await self.store.add_audit(
            AuditEntry(
                kind="pause",
                repo=repo,
                actor=actor,
                summary=("paused" if paused else "resumed")
                + (f": {reason}" if reason else ""),
            )
        )
        if not paused:
            await self.release_held(repo)

    # -- decisions -------------------------------------------------------------------

    async def _facts(self, action: Action, approved_by: str | None) -> PolicyFacts:
        now = self.clock.now()
        hour = now - timedelta(hours=1)
        executed = 0
        if action.number is not None and action.head_sha:
            records = await self.store.list_actions(
                repo=action.repo,
                number=action.number,
                kinds=[action.kind],
                statuses=[ActionStatus.EXECUTED, ActionStatus.INTENT],
            )
            executed = sum(
                1
                for r in records
                if r.action.id != action.id
                and same_commit(r.action.head_sha, action.head_sha)
            )
        count = self.store.count_actions
        statuses = [ActionStatus.EXECUTED, ActionStatus.INTENT, ActionStatus.QUEUED]
        return PolicyFacts(
            dev_mode=self.capped(action.repo),
            outside_sandbox=self.outside_sandbox(action.repo),
            paused=await self.paused(action.repo),
            approved_by=approved_by,
            author_is_team=action.hints.author_is_team,
            recommendation=action.hints.recommendation,
            changed_paths=action.hints.changed_paths,
            executed_on_sha=executed,
            public_writes_last_hour=await count(statuses, hour),
            repo_public_writes_last_hour=await count(statuses, hour, repo=action.repo),
            max_public_writes_per_hour=self.write_caps[0],
            max_repo_public_writes_per_hour=self.write_caps[1],
        )

    async def _profile_for(self, action: Action) -> RepoProfile | None:
        return await self.profiles.active(action.repo)

    async def submit(self, action: Action, force_propose: bool = False) -> ActionRecord:
        """Decide ``action`` and route it. Submitting the same action twice is a no-op.

        ``force_propose`` turns an ``execute`` decision into a proposal, for actions a
        maintainer asked for in chat: they see exactly what will run before it does.
        """
        profile = await self._profile_for(action)
        if profile is None:
            if action.kind.touches_repository:
                decision = Decision(
                    kind=DecisionKind.DENY,
                    rule="no_profile",
                    reason=f"{action.repo} has no approved profile",
                )
            else:
                decision = Decision(kind=DecisionKind.EXECUTE, rule="internal")
        else:
            decision = decide(action, profile, await self._facts(action, None))
        if (
            force_propose
            and decision.kind == DecisionKind.EXECUTE
            and action.kind.touches_repository
        ):
            decision = Decision(
                kind=DecisionKind.PROPOSE,
                rule="requested_in_chat",
                reason=decision.rule,
            )
        if (
            action.draft_for
            and action.kind.touches_repository
            and decision.kind in (DecisionKind.SHADOW, DecisionKind.EXECUTE)
            and not self.capped(action.repo)
        ):
            decision = Decision(
                kind=DecisionKind.PROPOSE,
                rule="requested_review",
                reason=f"draft for {action.draft_for}",
            )
        status = {
            DecisionKind.SHADOW: ActionStatus.SHADOWED,
            DecisionKind.DENY: ActionStatus.DENIED,
            DecisionKind.PROPOSE: ActionStatus.PROPOSED,
            DecisionKind.EXECUTE: ActionStatus.QUEUED,
        }[decision.kind]
        record, new = await self.store.add_action(
            ActionRecord(action=action, decision=decision, status=status)
        )
        if not new:
            return record
        await self.store.add_audit(
            AuditEntry(
                kind="decision",
                repo=action.repo,
                number=action.number,
                actor=action.provenance.requested_by or "service",
                action_id=action.id,
                summary=f"{action.kind}: {decision.kind} ({decision.rule})"
                + (f": {decision.reason}" if decision.reason else ""),
            )
        )
        if decision.kind == DecisionKind.PROPOSE:
            warnings = await self._warnings(action, profile)
            await self.store.add_proposal(
                Proposal(
                    action_id=action.id,
                    repo=action.repo,
                    warnings=warnings,
                    requested_by=action.draft_for,
                )
            )
            await self.bus.publish(
                Event(
                    kind=EventKind.PROPOSAL_CREATED,
                    repo=action.repo,
                    number=action.number,
                    payload={"action_id": action.id, "kind": action.kind},
                    source="pipeline",
                )
            )
        elif decision.kind == DecisionKind.EXECUTE:
            self.actuator.enqueue(action.id, action.repo, kind=action.kind)
        elif decision.kind == DecisionKind.DENY and decision.rule == WRITE_CAP_RULE:
            await self.set_paused(
                True, "service", reason="the hourly public write cap was reached"
            )
        return record

    async def _warnings(self, action: Action, profile: RepoProfile | None) -> list[str]:
        if profile is None or action.kind not in PUBLIC_TEXT_FIELDS:
            return []
        entities = await self.knowledge.internal_entities(profile.repo, profile)
        out: list[str] = []
        for text in _public_texts(action):
            result = check_text(
                text, profile, {"*"}, entities, tiers=action.kind not in TIER_BLIND
            )
            out += [
                f"blocked: {e}" for e in result.errors if "mentions @" not in e
            ] + result.warnings
        return sorted(set(out))

    # -- proposals ---------------------------------------------------------------------

    async def approve(
        self,
        proposal_id: str,
        login: str,
        edited_payload: dict[str, Any] | None = None,
        *,
        post_as: str | None = None,
    ) -> Proposal:
        """Approve a proposal. ``post_as`` (the approver's own login) posts a review
        draft with their GitHub account instead of the service's."""
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ApprovalError(f"proposal {proposal_id} does not exist")
        if proposal.state != ProposalState.PENDING:
            raise ApprovalError(
                f"proposal {proposal_id} is {proposal.state}, not pending"
            )
        record = await self.store.get_action(proposal.action_id)
        assert record is not None, f"proposal {proposal_id} points at a missing action"
        action = record.action
        if edited_payload is not None:
            await self._apply_edits(proposal, record, login, edited_payload)
        author = (
            action.payload.get("author") or None
            if action.kind == ActionKind.REVIEW_UPDATE
            else None
        )
        if action.kind == ActionKind.REVIEW_UPDATE and post_as != author:
            # GitHub lets only a review's author edit it.
            raise ApprovalError(
                f"{author} posted this review, so only they can change it, as themselves"
                if author
                else "the service posted this review, so it changes it under its own name"
            )
        if post_as is not None:
            if post_as != login or action.kind not in (
                ActionKind.REVIEW_POST,
                ActionKind.REVIEW_UPDATE,
                ActionKind.PR_FIX,
                ActionKind.PR_UPDATE_BRANCH,
                ActionKind.PR_APPROVE,
                ActionKind.CI_APPROVE_RUN,
            ):
                raise ApprovalError(
                    "only a review draft, a review's revision, a fix, a branch "
                    "update, or an approval of the PR can be done as its approver"
                )
            record.post_as = post_as
        elif action.kind == ActionKind.PR_FIX:
            raise ApprovalError("a fix PR is opened as the maintainer who approves it")
        elif action.kind == ActionKind.PR_APPROVE:
            raise ApprovalError("a PR is approved only by a person, as themselves")
        elif action.kind == ActionKind.CI_APPROVE_RUN:
            raise ApprovalError(
                "a held workflow is approved only by a person, as themselves"
            )
        proposal.state = ProposalState.APPROVED
        proposal.decided_by = login
        proposal.decided_at = self.clock.now()
        await self.store.save_proposal(proposal)
        await self._redecide(record, login)
        await self.bus.publish(
            Event(
                kind=EventKind.PROPOSAL_DECIDED,
                repo=action.repo,
                number=action.number,
                payload={
                    "proposal": proposal.id,
                    "action_id": action.id,
                    "state": "approved",
                    "by": login,
                },
            )
        )
        return proposal

    async def save_edits(
        self,
        proposal_id: str,
        login: str,
        edited_payload: dict[str, Any],
        *,
        person: bool = True,
    ) -> Proposal:
        """Keep a person's edits to a pending draft without deciding it, so they can
        come back to it; approving later posts what was saved. With ``person=False``
        the service revises its own draft (a reproduction's results added to a review):
        it is not marked edited and leaves no signal to learn from."""
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ApprovalError(f"proposal {proposal_id} does not exist")
        if proposal.state != ProposalState.PENDING:
            raise ApprovalError(
                f"proposal {proposal_id} is {proposal.state}, not pending"
            )
        record = await self.store.get_action(proposal.action_id)
        assert record is not None, f"proposal {proposal_id} points at a missing action"
        if await self._apply_edits(
            proposal, record, login, edited_payload, person=person
        ):
            # The leak check reads the text as it now is.
            proposal.warnings = await self._warnings(
                record.action, await self._profile_for(record.action)
            )
            await self.store.save_action(record)
            await self.store.save_proposal(proposal)
        return proposal

    async def _apply_edits(
        self,
        proposal: Proposal,
        record: ActionRecord,
        login: str,
        edited_payload: dict[str, Any],
        *,
        person: bool = True,
    ) -> bool:
        """Validate ``edited_payload`` against the action's payload model and put it
        in place; returns whether it changed anything."""
        action = record.action
        typed = PAYLOADS[action.kind].model_validate(edited_payload)
        new_payload = typed.model_dump(mode="json")
        if action.kind == ActionKind.REVIEW_UPDATE:
            # Which review is edited, and by whom, stay as drafted; the text changes.
            for key in ("target", "target_id", "kind", "revises", "author"):
                new_payload[key] = action.payload.get(key, new_payload[key])
        if new_payload == action.payload:
            return False
        if not person:
            action.payload = new_payload
            record.action = action
            return True
        await self.store.add_signal(
            Signal(
                kind=SignalKind.DRAFT_EDITED,
                repo=action.repo,
                number=action.number,
                source=login,
                payload={
                    "action_kind": action.kind,
                    "diff": _payload_diff(action.payload, new_payload),
                },
            )
        )
        action.payload = new_payload
        record.action = action
        proposal.edited = True
        return True

    async def _redecide(self, record: ActionRecord, login: str) -> None:
        action = record.action
        profile = await self._profile_for(action)
        if profile is None:
            record.status = ActionStatus.FAILED
            record.error = f"{action.repo} has no approved profile"
            await self.store.save_action(record)
            await self._back_to_reviewers(action.id, record.error)
            return
        decision = decide(action, profile, await self._facts(action, login))
        if (
            action.draft_for
            and decision.kind == DecisionKind.SHADOW
            and not self.capped(action.repo)
        ):
            # A reviewer asked for this draft and a maintainer approved posting it.
            decision = Decision(
                kind=DecisionKind.EXECUTE,
                rule="requested_review",
                reason=f"draft for {action.draft_for}, approved by {login}",
            )
        record.decision = decision
        record.approver = login
        if decision.kind == DecisionKind.EXECUTE:
            record.status = ActionStatus.QUEUED
            await self.store.save_action(record)
            self.actuator.enqueue(action.id, action.repo, login, action.kind)
        else:
            record.status = ActionStatus.PROPOSED
            record.error = f"held after approval: {decision.reason or decision.rule}"
            await self.store.save_action(record)
            if decision.rule != PAUSED_RULE:  # a pause holds it until released
                await self._back_to_reviewers(action.id, record.error)
        await self.store.add_audit(
            AuditEntry(
                kind="proposal",
                repo=action.repo,
                number=action.number,
                actor=login,
                action_id=action.id,
                summary=f"approved; {decision.kind} ({decision.rule})",
            )
        )

    async def _back_to_reviewers(self, action_id: str, why: str) -> None:
        """Return an approved proposal that will not run to pending, with ``why``
        among its warnings, so its reviewers see it and can change or reject it."""
        proposal = await self.store.proposal_for_action(action_id)
        if proposal is None or proposal.state != ProposalState.APPROVED:
            return
        proposal.state = ProposalState.PENDING
        proposal.decided_by = proposal.decided_at = None
        proposal.warnings = [
            *(w for w in proposal.warnings if not w.startswith("not done: ")),
            f"not done: {why}",
        ]
        await self.store.save_proposal(proposal)

    async def reject(self, proposal_id: str, login: str, reason: str) -> Proposal:
        if not reason.strip():
            raise ApprovalError("a rejection needs a reason")
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None or proposal.state != ProposalState.PENDING:
            raise ApprovalError(f"proposal {proposal_id} is not pending")
        record = await self.store.get_action(proposal.action_id)
        proposal.state = ProposalState.REJECTED
        proposal.decided_by = login
        proposal.decided_at = self.clock.now()
        proposal.reason = reason
        await self.store.save_proposal(proposal)
        if record is not None:
            record.status = ActionStatus.DENIED
            record.error = f"rejected by {login}: {reason}"
            await self.store.save_action(record)
            await self.store.add_signal(
                Signal(
                    kind=SignalKind.PROPOSAL_REJECTED,
                    repo=record.action.repo,
                    number=record.action.number,
                    source=login,
                    payload={"action_kind": record.action.kind, "reason": reason},
                )
            )
            await self.store.add_audit(
                AuditEntry(
                    kind="proposal",
                    repo=record.action.repo,
                    number=record.action.number,
                    actor=login,
                    action_id=record.action.id,
                    summary=f"rejected: {reason}",
                )
            )
            await self.bus.publish(
                Event(
                    kind=EventKind.PROPOSAL_DECIDED,
                    repo=record.action.repo,
                    number=record.action.number,
                    payload={
                        "proposal": proposal.id,
                        "action_id": record.action.id,
                        "state": "rejected",
                        "by": login,
                    },
                )
            )
        return proposal

    async def supersede_stale(self, repo: str, number: int, head_sha: str) -> int:
        """Mark pending proposals for an older head of ``repo#number`` as superseded.

        A review draft a reviewer asked for stays: it reviews its own commit, and
        posts there when approved (``HandlerSpec.approved_on_its_commit``).
        """
        count = 0
        for proposal in await self.store.list_proposals(ProposalState.PENDING, repo):
            record = await self.store.get_action(proposal.action_id)
            if (
                record is None
                or record.action.number != number
                or not record.action.head_sha
                or (
                    record.action.kind == ActionKind.REVIEW_POST
                    and record.action.draft_for
                )
            ):
                continue
            if not same_commit(record.action.head_sha, head_sha):
                proposal.state = ProposalState.SUPERSEDED
                await self.store.save_proposal(proposal)
                record.status = ActionStatus.SUPERSEDED
                await self.store.save_action(record)
                count += 1
        return count

    async def release_held(self, repo: str | None = None) -> int:
        """Re-decide approved proposals that were held, for example while paused."""
        released = 0
        for proposal in await self.store.list_proposals(ProposalState.APPROVED, repo):
            record = await self.store.get_action(proposal.action_id)
            if (
                record
                and record.status == ActionStatus.PROPOSED
                and proposal.decided_by
            ):
                await self._redecide(record, proposal.decided_by)
                released += 1
        return released


def _public_texts(action: Action) -> list[str]:
    texts = []
    for field in PUBLIC_TEXT_FIELDS.get(action.kind, ()):
        if field.endswith("[].body"):
            texts += [
                c.get("body", "") for c in action.payload.get(field.split("[]")[0], [])
            ]
        elif isinstance(action.payload.get(field), str):
            texts.append(action.payload[field])
    return texts


def _payload_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    a = json.dumps(old, indent=1, sort_keys=True, ensure_ascii=False).splitlines()
    b = json.dumps(new, indent=1, sort_keys=True, ensure_ascii=False).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "draft", "approved", lineterm=""))[
        :20_000
    ]
