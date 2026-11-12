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

"""The SQLite store: one writer connection in WAL mode, migrated with Alembic on open."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import (
    Float,
    and_,
    cast,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..domain.actions import ActionKind, Proposal, ProposalState
from ..domain.agents import AgentQuestion, QuestionState
from ..domain.clock import as_utc
from ..domain.memory import MemoryChange, MemoryChangeState, Signal
from ..domain.merge_gate import MergeArm
from ..domain.ports import UserRecord
from ..domain.records import (
    ActionRecord,
    AgentRunRecord,
    AuditEntry,
    ChatMessage,
    ChatThread,
    CiObservation,
    Job,
    JobStatus,
    PersonAlias,
    PrAssignee,
    ProfileState,
    RepoRecord,
    ReviewRecord,
)
from ..domain.scoring import Evaluation
from ..domain.snapshot import PrSnapshot, PrState
from . import models as m

M = TypeVar("M", bound=BaseModel)

ACTIVE_JOB_STATES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING)


def ts(value: datetime) -> str:
    """A sortable UTC timestamp string."""
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _assignee(row: Row) -> PrAssignee:
    return PrAssignee(
        repo=row.repo,
        number=row.number,
        login=row.login,
        role=row.role,
        kind=row.kind,
        by=row.by,
        at=datetime.fromisoformat(row.at),
    )


def _dump(model: BaseModel) -> str:
    return model.model_dump_json()


def _load(cls: type[M], row: Row | None, column: str = "data") -> M | None:
    if row is None:
        return None
    return cls.model_validate_json(getattr(row, column))


def _many(cls: type[M], rows: list[Row]) -> list[M]:
    return [cls.model_validate_json(r.data) for r in rows]


def run_migrations(db_path: Path) -> None:
    """Bring the database schema up to date (synchronously, before serving)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


class SqliteStore:
    """Implements ``domain.ports.Store``."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._write = asyncio.Lock()

    @classmethod
    async def open(cls, db_path: Path) -> SqliteStore:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(run_migrations, db_path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return cls(engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def _exec(self, *stmts: Any) -> None:
        async with self._write, self._engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(stmt)

    async def _fetch(self, stmt: Any) -> list[Row]:
        async with self._engine.connect() as conn:
            return list((await conn.execute(stmt)).fetchall())

    async def _one(self, stmt: Any) -> Row | None:
        rows = await self._fetch(stmt)
        return rows[0] if rows else None

    async def _upsert(
        self, table: Any, keys: dict[str, Any], values: dict[str, Any]
    ) -> None:
        cond = and_(*[table.c[k] == v for k, v in keys.items()])
        async with self._write, self._engine.begin() as conn:
            result = await conn.execute(update(table).where(cond).values(**values))
            if result.rowcount == 0:
                await conn.execute(insert(table).values(**keys, **values))

    # -- repositories and profiles ------------------------------------------------

    async def save_repo(self, record: RepoRecord) -> None:
        await self._upsert(
            m.repos,
            {"repo": record.repo},
            {"active": record.active, "paused": record.paused, "data": _dump(record)},
        )

    async def get_repo(self, repo: str) -> RepoRecord | None:
        return _load(
            RepoRecord,
            await self._one(select(m.repos.c.data).where(m.repos.c.repo == repo)),
        )

    async def list_repos(self) -> list[RepoRecord]:
        return _many(
            RepoRecord,
            await self._fetch(select(m.repos.c.data).order_by(m.repos.c.repo)),
        )

    async def get_profile_state(self, repo: str) -> ProfileState | None:
        return _load(
            ProfileState,
            await self._one(
                select(m.profile_states.c.data).where(m.profile_states.c.repo == repo)
            ),
        )

    async def save_profile_state(self, state: ProfileState) -> None:
        await self._upsert(
            m.profile_states, {"repo": state.repo}, {"data": _dump(state)}
        )

    # -- snapshots and evaluations -------------------------------------------------

    async def get_snapshot(self, repo: str, number: int) -> PrSnapshot | None:
        stmt = select(m.pr_snapshots.c.data).where(
            m.pr_snapshots.c.repo == repo, m.pr_snapshots.c.number == number
        )
        return _load(PrSnapshot, await self._one(stmt))

    async def save_snapshot(self, pr: PrSnapshot) -> None:
        await self._upsert(
            m.pr_snapshots,
            {"repo": pr.repo, "number": pr.number},
            {
                "state": pr.state.value,
                "head_sha": pr.head_sha,
                "updated_at": ts(pr.updated_at) if pr.updated_at else "",
                "data": _dump(pr),
            },
        )

    async def list_snapshots(
        self, repo: str | None = None, open_only: bool = True
    ) -> list[PrSnapshot]:
        stmt = select(m.pr_snapshots.c.data)
        if repo:
            stmt = stmt.where(m.pr_snapshots.c.repo == repo)
        if open_only:
            stmt = stmt.where(m.pr_snapshots.c.state == PrState.OPEN.value)
        return _many(
            PrSnapshot,
            await self._fetch(
                stmt.order_by(m.pr_snapshots.c.repo, m.pr_snapshots.c.number.desc())
            ),
        )

    async def get_evaluation(self, repo: str, number: int) -> Evaluation | None:
        stmt = select(m.evaluations.c.data).where(
            m.evaluations.c.repo == repo, m.evaluations.c.number == number
        )
        return _load(Evaluation, await self._one(stmt))

    async def save_evaluation(self, evaluation: Evaluation) -> None:
        await self._upsert(
            m.evaluations,
            {"repo": evaluation.repo, "number": evaluation.number},
            {
                "tier": evaluation.tier,
                "total": f"{evaluation.total:06.1f}",
                "data": _dump(evaluation),
            },
        )

    async def list_evaluations(self, repo: str | None = None) -> list[Evaluation]:
        stmt = select(m.evaluations.c.data)
        if repo:
            stmt = stmt.where(m.evaluations.c.repo == repo)
        # ``total`` is stored as text; order by its number, not its spelling.
        return _many(
            Evaluation,
            await self._fetch(stmt.order_by(cast(m.evaluations.c.total, Float).desc())),
        )

    # -- jobs --------------------------------------------------------------------

    def _job_values(self, job: Job) -> dict[str, Any]:
        return {
            "playbook": job.playbook,
            "key": job.key,
            "status": job.status.value,
            "pool": job.pool,
            "repo": job.repo,
            "number": job.number,
            "lease_until": ts(job.lease_until) if job.lease_until else None,
            "not_before": ts(job.not_before) if job.not_before else None,
            "created_at": ts(job.created_at),
            "updated_at": ts(job.updated_at),
            "data": _dump(job),
        }

    async def upsert_job(self, job: Job) -> tuple[Job, bool]:
        async with self._write, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(m.jobs.c.data).where(
                        m.jobs.c.playbook == job.playbook, m.jobs.c.key == job.key
                    )
                )
            ).first()
            if row is None:
                await conn.execute(
                    insert(m.jobs).values(id=job.id, **self._job_values(job))
                )
                return job, True
            existing = Job.model_validate_json(row.data)
            if existing.status in ACTIVE_JOB_STATES and job.events:
                existing.events = (existing.events + job.events)[-20:]
                if existing.status == JobStatus.WAITING:
                    existing.status = JobStatus.QUEUED
                    existing.wait = None
                existing.updated_at = job.updated_at
                await conn.execute(
                    update(m.jobs)
                    .where(m.jobs.c.id == existing.id)
                    .values(**self._job_values(existing))
                )
            return existing, False

    async def get_job(self, job_id: str) -> Job | None:
        return _load(
            Job, await self._one(select(m.jobs.c.data).where(m.jobs.c.id == job_id))
        )

    async def save_job(self, job: Job) -> None:
        await self._exec(
            update(m.jobs).where(m.jobs.c.id == job.id).values(**self._job_values(job))
        )

    async def claim_jobs(
        self,
        pool: str,
        limit: int,
        lease_seconds: int,
        now: datetime,
        *,
        asked: bool | None = None,
    ) -> list[Job]:
        now_s = ts(now)
        async with self._write, self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(m.jobs.c.data)
                    .where(
                        m.jobs.c.status == JobStatus.QUEUED.value,
                        m.jobs.c.pool == pool,
                        or_(
                            m.jobs.c.not_before.is_(None), m.jobs.c.not_before <= now_s
                        ),
                    )
                    .order_by(m.jobs.c.created_at)
                )
            ).fetchall()
            ready = [Job.model_validate_json(row.data) for row in rows]
            if asked is not None:
                ready = [j for j in ready if (j.asked_by is not None) == asked]
            # People's requests first, each kind oldest first (the sort is stable).
            ready.sort(key=lambda j: j.asked_by is None)
            claimed = []
            for job in ready[:limit]:
                job.status = JobStatus.RUNNING
                job.attempts += 1
                job.lease_until = now + timedelta(seconds=lease_seconds)
                job.updated_at = now
                await conn.execute(
                    update(m.jobs)
                    .where(m.jobs.c.id == job.id)
                    .values(**self._job_values(job))
                )
                claimed.append(job)
            return claimed

    async def list_jobs(
        self,
        statuses: list[JobStatus] | None = None,
        repo: str | None = None,
        limit: int = 200,
    ) -> list[Job]:
        stmt = select(m.jobs.c.data)
        if statuses:
            stmt = stmt.where(m.jobs.c.status.in_([s.value for s in statuses]))
        if repo:
            stmt = stmt.where(m.jobs.c.repo == repo)
        return _many(
            Job,
            await self._fetch(stmt.order_by(m.jobs.c.updated_at.desc()).limit(limit)),
        )

    async def requeue_expired(self, now: datetime) -> int:
        rows = await self._fetch(
            select(m.jobs.c.data).where(
                m.jobs.c.status == JobStatus.RUNNING.value,
                m.jobs.c.lease_until < ts(now),
            )
        )
        for row in rows:
            job = Job.model_validate_json(row.data)
            job.status = (
                JobStatus.QUEUED
                if job.attempts < job.max_attempts
                else JobStatus.FAILED
            )
            job.error = job.error or "lease expired"
            job.lease_until = None
            job.updated_at = now
            await self.save_job(job)
        return len(rows)

    # -- agent runs ----------------------------------------------------------------

    async def add_agent_run(self, run: AgentRunRecord) -> None:
        await self._exec(
            insert(m.agent_runs).values(
                id=run.id,
                job_id=run.job_id,
                repo=run.repo,
                runtime=run.runtime,
                created_at=ts(run.created_at),
                data=_dump(run),
            )
        )

    async def list_agent_runs(
        self,
        limit: int = 100,
        runtime: str | None = None,
        *,
        repo: str | None = None,
        since: datetime | None = None,
    ) -> list[AgentRunRecord]:
        stmt = select(m.agent_runs.c.data)
        if runtime:
            stmt = stmt.where(m.agent_runs.c.runtime == runtime)
        if repo:
            stmt = stmt.where(m.agent_runs.c.repo == repo)
        if since:
            stmt = stmt.where(m.agent_runs.c.created_at >= ts(since))
        return _many(
            AgentRunRecord,
            await self._fetch(
                stmt.order_by(m.agent_runs.c.created_at.desc()).limit(limit)
            ),
        )

    async def save_question(self, question: AgentQuestion) -> None:
        values = {
            "repo": question.repo,
            "state": question.state.value,
            "created_at": ts(question.created_at) if question.created_at else "",
            "data": _dump(question),
        }
        existing = await self._one(
            select(m.agent_questions.c.id).where(m.agent_questions.c.id == question.id)
        )
        if existing is None:
            await self._exec(insert(m.agent_questions).values(id=question.id, **values))
        else:
            await self._exec(
                update(m.agent_questions)
                .where(m.agent_questions.c.id == question.id)
                .values(**values)
            )

    async def get_question(self, question_id: str) -> AgentQuestion | None:
        return _load(
            AgentQuestion,
            await self._one(
                select(m.agent_questions.c.data).where(
                    m.agent_questions.c.id == question_id
                )
            ),
        )

    async def list_questions(
        self, state: QuestionState | None = None, repo: str | None = None
    ) -> list[AgentQuestion]:
        stmt = select(m.agent_questions.c.data)
        if state:
            stmt = stmt.where(m.agent_questions.c.state == state.value)
        if repo:
            stmt = stmt.where(m.agent_questions.c.repo == repo)
        return _many(
            AgentQuestion,
            await self._fetch(stmt.order_by(m.agent_questions.c.created_at.desc())),
        )

    async def agent_usage_since(
        self, runtime: str, since: datetime
    ) -> tuple[int, float]:
        rows = await self._fetch(
            select(m.agent_runs.c.data).where(
                m.agent_runs.c.runtime == runtime,
                m.agent_runs.c.created_at >= ts(since),
            )
        )
        # Runs on a person's own account are theirs, not the service's to budget.
        runs = [r for r in _many(AgentRunRecord, rows) if r.account is None]
        return len(runs), sum(r.cost_usd or 0.0 for r in runs)

    # -- actions and proposals --------------------------------------------------------

    def _action_values(self, record: ActionRecord) -> dict[str, Any]:
        a = record.action
        return {
            "idem_key": a.idempotency_key,
            "repo": a.repo,
            "number": a.number,
            "kind": a.kind.value,
            "status": record.status.value,
            "created_at": ts(a.created_at),
            "updated_at": ts(record.updated_at),
            "data": _dump(record),
        }

    async def add_action(self, record: ActionRecord) -> tuple[ActionRecord, bool]:
        try:
            await self._exec(
                insert(m.actions).values(
                    id=record.action.id, **self._action_values(record)
                )
            )
            return record, True
        except IntegrityError:
            row = await self._one(
                select(m.actions.c.data).where(
                    m.actions.c.idem_key == record.action.idempotency_key
                )
            )
            existing = _load(ActionRecord, row)
            assert existing is not None, (
                f"expected an action with key {record.action.idempotency_key}"
            )
            return existing, False

    async def get_action(self, action_id: str) -> ActionRecord | None:
        return _load(
            ActionRecord,
            await self._one(
                select(m.actions.c.data).where(m.actions.c.id == action_id)
            ),
        )

    async def save_action(self, record: ActionRecord) -> None:
        await self._exec(
            update(m.actions)
            .where(m.actions.c.id == record.action.id)
            .values(**self._action_values(record))
        )

    async def list_actions(
        self,
        repo: str | None = None,
        number: int | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[ActionRecord]:
        stmt = select(m.actions.c.data)
        if repo:
            stmt = stmt.where(m.actions.c.repo == repo)
        if number is not None:
            stmt = stmt.where(m.actions.c.number == number)
        if kinds:
            stmt = stmt.where(m.actions.c.kind.in_([str(k) for k in kinds]))
        if statuses:
            stmt = stmt.where(m.actions.c.status.in_([str(s) for s in statuses]))
        if since:
            stmt = stmt.where(m.actions.c.updated_at >= ts(since))
        return _many(
            ActionRecord,
            await self._fetch(
                stmt.order_by(m.actions.c.created_at.desc()).limit(limit)
            ),
        )

    async def count_actions(
        self,
        statuses: list[str],
        since: datetime,
        repo: str | None = None,
        public_only: bool = True,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(m.actions)
            .where(
                m.actions.c.status.in_(statuses), m.actions.c.updated_at >= ts(since)
            )
        )
        if repo:
            stmt = stmt.where(m.actions.c.repo == repo)
        if public_only:
            stmt = stmt.where(
                m.actions.c.kind.notin_(
                    [k.value for k in ActionKind if not k.touches_repository]
                )
            )
        row = await self._one(stmt)
        return int(row[0]) if row else 0

    async def add_proposal(self, proposal: Proposal) -> None:
        await self._exec(
            insert(m.proposals).values(
                id=proposal.id,
                action_id=proposal.action_id,
                repo=proposal.repo,
                state=proposal.state.value,
                created_at=ts(proposal.created_at),
                data=_dump(proposal),
            )
        )

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        return _load(
            Proposal,
            await self._one(
                select(m.proposals.c.data).where(m.proposals.c.id == proposal_id)
            ),
        )

    async def proposal_for_action(self, action_id: str) -> Proposal | None:
        return _load(
            Proposal,
            await self._one(
                select(m.proposals.c.data).where(m.proposals.c.action_id == action_id)
            ),
        )

    async def save_proposal(self, proposal: Proposal) -> None:
        await self._exec(
            update(m.proposals)
            .where(m.proposals.c.id == proposal.id)
            .values(state=proposal.state.value, data=_dump(proposal))
        )

    async def list_proposals(
        self, state: ProposalState | None = None, repo: str | None = None
    ) -> list[Proposal]:
        stmt = select(m.proposals.c.data)
        if state:
            stmt = stmt.where(m.proposals.c.state == state.value)
        if repo:
            stmt = stmt.where(m.proposals.c.repo == repo)
        return _many(
            Proposal, await self._fetch(stmt.order_by(m.proposals.c.created_at.desc()))
        )

    # -- learning ------------------------------------------------------------------

    async def add_signal(self, signal: Signal) -> None:
        await self._exec(
            insert(m.signals).values(
                id=signal.id,
                repo=signal.repo,
                kind=signal.kind.value,
                consumed=signal.consumed_by is not None,
                created_at=ts(signal.created_at),
                data=_dump(signal),
            )
        )

    async def list_signals(
        self, repo: str | None = None, unconsumed: bool = True
    ) -> list[Signal]:
        stmt = select(m.signals.c.data)
        if repo:
            stmt = stmt.where(m.signals.c.repo == repo)
        if unconsumed:
            stmt = stmt.where(m.signals.c.consumed.is_(False))
        return _many(Signal, await self._fetch(stmt.order_by(m.signals.c.created_at)))

    async def save_signal(self, signal: Signal) -> None:
        await self._exec(
            update(m.signals)
            .where(m.signals.c.id == signal.id)
            .values(consumed=signal.consumed_by is not None, data=_dump(signal))
        )

    async def add_review_record(self, record: ReviewRecord) -> None:
        await self._exec(
            insert(m.review_records).values(
                repo=record.repo,
                number=record.number,
                created_at=ts(record.created_at),
                data=_dump(record),
            )
        )

    async def revise_review_record(
        self, repo: str, number: int, action_id: str, result: dict[str, Any]
    ) -> bool:
        stmt = select(m.review_records.c.id, m.review_records.c.data).where(
            m.review_records.c.repo == repo, m.review_records.c.number == number
        )
        for row in await self._fetch(stmt):
            record = ReviewRecord.model_validate_json(row.data)
            if record.action_id != action_id:
                continue
            record.result = result
            await self._exec(
                update(m.review_records)
                .where(m.review_records.c.id == row.id)
                .values(data=_dump(record))
            )
            return True
        return False

    async def list_review_records(self, repo: str, number: int) -> list[ReviewRecord]:
        stmt = (
            select(m.review_records.c.data)
            .where(m.review_records.c.repo == repo, m.review_records.c.number == number)
            .order_by(m.review_records.c.id)
        )
        return _many(ReviewRecord, await self._fetch(stmt))

    async def add_ci_observation(self, obs: CiObservation) -> None:
        await self._exec(
            insert(m.ci_observations).values(
                id=obs.id,
                repo=obs.repo,
                sha=obs.sha,
                created_at=ts(obs.created_at),
                data=_dump(obs),
            )
        )

    async def set_ci_verdict(
        self, obs_id: str, verdict: str | None, by: str | None, at: datetime | None
    ) -> CiObservation | None:
        async with self._write, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(m.ci_observations.c.data).where(
                        m.ci_observations.c.id == obs_id
                    )
                )
            ).first()
            if row is None:
                return None
            obs = CiObservation.model_validate_json(row[0])
            obs = obs.model_copy(
                update={"verdict": verdict, "verdict_by": by, "verdict_at": at}
            )
            await conn.execute(
                update(m.ci_observations)
                .where(m.ci_observations.c.id == obs_id)
                .values(data=_dump(obs))
            )
            return obs

    async def list_ci_observations(
        self,
        repo: str | None = None,
        sha: str | None = None,
        since: datetime | None = None,
    ) -> list[CiObservation]:
        stmt = select(m.ci_observations.c.data)
        if repo:
            stmt = stmt.where(m.ci_observations.c.repo == repo)
        if sha:
            stmt = stmt.where(m.ci_observations.c.sha == sha)
        if since:
            stmt = stmt.where(m.ci_observations.c.created_at >= ts(since))
        return _many(
            CiObservation,
            await self._fetch(stmt.order_by(m.ci_observations.c.created_at)),
        )

    # -- merge arms, assignees, leases -----------------------------------------------

    async def save_arm(self, arm: MergeArm) -> None:
        await self._upsert(
            m.merge_arms, {"repo": arm.repo, "number": arm.number}, {"data": _dump(arm)}
        )

    async def get_arm(self, repo: str, number: int) -> MergeArm | None:
        stmt = select(m.merge_arms.c.data).where(
            m.merge_arms.c.repo == repo, m.merge_arms.c.number == number
        )
        return _load(MergeArm, await self._one(stmt))

    async def list_arms(self, repo: str) -> list[MergeArm]:
        stmt = (
            select(m.merge_arms.c.data)
            .where(m.merge_arms.c.repo == repo)
            .order_by(m.merge_arms.c.number)
        )
        return _many(MergeArm, await self._fetch(stmt))

    async def delete_arm(self, repo: str, number: int) -> None:
        await self._exec(
            delete(m.merge_arms).where(
                m.merge_arms.c.repo == repo, m.merge_arms.c.number == number
            )
        )

    async def add_assignee(self, assignee: PrAssignee) -> PrAssignee:
        a = assignee
        async with self._write, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(m.pr_assignees).where(
                        m.pr_assignees.c.repo == a.repo,
                        m.pr_assignees.c.number == a.number,
                        func.lower(m.pr_assignees.c.login) == a.login.lower(),
                        m.pr_assignees.c.role == a.role,
                    )
                )
            ).first()
            if row is not None:
                kept = _assignee(row)
                if kept.kind == "assigned" or a.kind == "claimed":
                    return kept
                await conn.execute(
                    update(m.pr_assignees)
                    .where(
                        m.pr_assignees.c.repo == a.repo,
                        m.pr_assignees.c.number == a.number,
                        m.pr_assignees.c.login == kept.login,
                        m.pr_assignees.c.role == a.role,
                    )
                    .values(kind=a.kind, by=a.by, at=ts(a.at))
                )
                return kept.model_copy(update={"kind": a.kind, "by": a.by, "at": a.at})
            await conn.execute(
                insert(m.pr_assignees).values(
                    repo=a.repo,
                    number=a.number,
                    login=a.login,
                    role=a.role,
                    kind=a.kind,
                    by=a.by,
                    at=ts(a.at),
                )
            )
            return a

    async def remove_assignee(
        self, repo: str, number: int, login: str, role: str
    ) -> bool:
        async with self._write, self._engine.begin() as conn:
            result = await conn.execute(
                delete(m.pr_assignees).where(
                    m.pr_assignees.c.repo == repo,
                    m.pr_assignees.c.number == number,
                    func.lower(m.pr_assignees.c.login) == login.lower(),
                    m.pr_assignees.c.role == role,
                )
            )
            return bool(result.rowcount)

    async def list_assignees(
        self, repo: str | None = None, number: int | None = None
    ) -> list[PrAssignee]:
        stmt = select(m.pr_assignees)
        if repo is not None:
            stmt = stmt.where(m.pr_assignees.c.repo == repo)
        if number is not None:
            stmt = stmt.where(m.pr_assignees.c.number == number)
        rows = await self._fetch(stmt.order_by(m.pr_assignees.c.at))
        return [_assignee(r) for r in rows]

    async def list_aliases(self) -> list[PersonAlias]:
        rows = await self._fetch(
            select(m.person_aliases).order_by(m.person_aliases.c.login)
        )
        return [
            PersonAlias(
                login=r.display_login,
                alias=r.alias,
                set_by=r.set_by,
                updated_at=datetime.fromisoformat(r.updated_at),
            )
            for r in rows
        ]

    async def set_alias(self, alias: PersonAlias) -> None:
        key = alias.login.lower()
        async with self._write, self._engine.begin() as conn:
            await conn.execute(
                delete(m.person_aliases).where(m.person_aliases.c.login == key)
            )
            await conn.execute(
                insert(m.person_aliases).values(
                    login=key,
                    display_login=alias.login,
                    alias=alias.alias,
                    set_by=alias.set_by,
                    updated_at=ts(alias.updated_at),
                )
            )

    async def delete_alias(self, login: str) -> None:
        await self._exec(
            delete(m.person_aliases).where(m.person_aliases.c.login == login.lower())
        )

    async def acquire_lease(
        self, lab: str, resource: str, holder: str, ttl_seconds: int, now: datetime
    ) -> bool:
        expires = ts(now + timedelta(seconds=ttl_seconds))
        async with self._write, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(m.leases).where(
                        m.leases.c.lab == lab, m.leases.c.resource == resource
                    )
                )
            ).first()
            if row is None:
                await conn.execute(
                    insert(m.leases).values(
                        lab=lab, resource=resource, holder=holder, expires_at=expires
                    )
                )
                return True
            if row.holder == holder or row.expires_at < ts(now):
                await conn.execute(
                    update(m.leases)
                    .where(m.leases.c.lab == lab, m.leases.c.resource == resource)
                    .values(holder=holder, expires_at=expires)
                )
                return True
            return False

    async def release_lease(self, lab: str, resource: str, holder: str) -> None:
        await self._exec(
            delete(m.leases).where(
                m.leases.c.lab == lab,
                m.leases.c.resource == resource,
                m.leases.c.holder == holder,
            )
        )

    # -- memory changes ------------------------------------------------------------

    async def save_memory_change(self, change: MemoryChange) -> None:
        await self._upsert(
            m.memory_changes,
            {"id": change.id},
            {
                "state": change.state.value,
                "created_at": ts(change.created_at),
                "data": _dump(change),
            },
        )

    async def get_memory_change(self, change_id: str) -> MemoryChange | None:
        return _load(
            MemoryChange,
            await self._one(
                select(m.memory_changes.c.data).where(
                    m.memory_changes.c.id == change_id
                )
            ),
        )

    async def list_memory_changes(
        self, state: MemoryChangeState | None = None
    ) -> list[MemoryChange]:
        stmt = select(m.memory_changes.c.data)
        if state:
            stmt = stmt.where(m.memory_changes.c.state == state.value)
        return _many(
            MemoryChange,
            await self._fetch(stmt.order_by(m.memory_changes.c.created_at.desc())),
        )

    # -- chat ----------------------------------------------------------------------

    async def save_thread(self, thread: ChatThread) -> None:
        await self._upsert(
            m.chat_threads,
            {"id": thread.id},
            {
                "owner": thread.owner,
                "created_at": ts(thread.created_at),
                "data": _dump(thread),
            },
        )

    async def get_thread(self, thread_id: str) -> ChatThread | None:
        return _load(
            ChatThread,
            await self._one(
                select(m.chat_threads.c.data).where(m.chat_threads.c.id == thread_id)
            ),
        )

    async def list_threads(self, owner: str) -> list[ChatThread]:
        stmt = (
            select(m.chat_threads.c.data)
            .where(m.chat_threads.c.owner == owner)
            .order_by(m.chat_threads.c.created_at.desc())
        )
        return _many(ChatThread, await self._fetch(stmt))

    async def add_message(self, message: ChatMessage) -> None:
        await self._exec(
            insert(m.chat_messages).values(
                id=message.id,
                thread_id=message.thread_id,
                created_at=ts(message.created_at),
                data=_dump(message),
            )
        )

    async def rename_repo(self, old: str, new: str) -> int:
        """Give every record of repository ``old`` the name ``new``: the repository
        columns, and the name where it stands whole in stored data and keys (jobs'
        keys, idempotency keys, memory paths). Returns the rows changed."""
        pattern = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(old)}(?![A-Za-z0-9_.-])")
        changed = 0
        async with self._write, self._engine.begin() as conn:
            for table in m.metadata.sorted_tables:
                cols = table.c
                text_cols = [
                    c for c in ("data", "key", "idem_key", "value") if c in cols
                ]
                if "repo" not in cols and not text_cols:
                    continue
                conditions = [func.instr(cols[c], old) > 0 for c in text_cols]
                if "repo" in cols:
                    conditions.append(cols["repo"] == old)
                keys = list(table.primary_key.columns)
                if not keys:  # a row could not be singled out to update
                    continue
                rows = (
                    await conn.execute(
                        select(
                            *keys,
                            *(cols[c] for c in text_cols),
                            *([cols["repo"]] if "repo" in cols else []),
                        ).where(or_(*conditions))
                    )
                ).all()
                for row in rows:
                    values: dict[str, Any] = {}
                    for c in text_cols:
                        text = row._mapping[cols[c]]
                        if isinstance(text, str):
                            renamed = pattern.sub(new, text)
                            if renamed != text:
                                values[c] = renamed
                    if "repo" in cols and row._mapping[cols["repo"]] == old:
                        values["repo"] = new
                    if values:
                        where = and_(*(k == row._mapping[k] for k in keys))
                        await conn.execute(update(table).where(where).values(**values))
                        changed += 1
        return changed

    async def messages_with_pending_requests(self) -> list[ChatMessage]:
        """Messages in any conversation with a request card still waiting."""
        stmt = select(m.chat_messages.c.data).where(
            m.chat_messages.c.data.like('%"state":"pending"%')
        )
        return [
            msg
            for msg in _many(ChatMessage, await self._fetch(stmt))
            if any(r.state == "pending" for r in msg.requests)
        ]

    async def save_message(self, message: ChatMessage) -> None:
        await self._exec(
            update(m.chat_messages)
            .where(m.chat_messages.c.id == message.id)
            .values(data=_dump(message))
        )

    async def list_said(
        self, limit: int = 20_000
    ) -> list[tuple[str, ChatThread | None, ChatMessage]]:
        rows = await self._fetch(
            select(m.chat_messages.c.data)
            .order_by(m.chat_messages.c.created_at.desc())
            .limit(limit * 4)
        )
        said = [
            msg
            for msg in reversed(_many(ChatMessage, rows))
            if msg.role == "user" and msg.text.strip()
        ][-limit:]
        threads: dict[str, ChatThread | None] = {}
        out = []
        for msg in said:
            if msg.thread_id not in threads:
                threads[msg.thread_id] = await self.get_thread(msg.thread_id)
            thread = threads[msg.thread_id]
            who = msg.author or (thread.owner if thread else None)
            if who:
                out.append((who, thread, msg))
        return out

    async def list_messages(self, thread_id: str) -> list[ChatMessage]:
        stmt = (
            select(m.chat_messages.c.data)
            .where(m.chat_messages.c.thread_id == thread_id)
            .order_by(m.chat_messages.c.created_at)
        )
        return _many(ChatMessage, await self._fetch(stmt))

    # -- users, audit, state ---------------------------------------------------------

    async def save_user(self, user: UserRecord) -> None:
        await self._upsert(m.users, {"login": user.login}, {"data": _dump(user)})

    async def get_user(self, login: str) -> UserRecord | None:
        return _load(
            UserRecord,
            await self._one(select(m.users.c.data).where(m.users.c.login == login)),
        )

    async def add_audit(self, entry: AuditEntry) -> None:
        await self._exec(
            insert(m.audit_log).values(
                id=entry.id,
                repo=entry.repo,
                created_at=ts(entry.created_at),
                data=_dump(entry),
            )
        )

    async def list_audit(
        self, repo: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        stmt = select(m.audit_log.c.data)
        if repo:
            stmt = stmt.where(m.audit_log.c.repo == repo)
        return _many(
            AuditEntry,
            await self._fetch(
                stmt.order_by(m.audit_log.c.created_at.desc()).limit(limit)
            ),
        )

    async def get_state(self, key: str) -> Any | None:
        row = await self._one(
            select(m.service_state.c.value).where(m.service_state.c.key == key)
        )
        return json.loads(row.value) if row else None

    async def set_state(self, key: str, value: Any) -> None:
        await self._upsert(
            m.service_state, {"key": key}, {"value": json.dumps(value, default=str)}
        )
