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

"""SQLite tables (design 7.1).

Each row keeps the record as JSON in ``data``; the other columns are the ones that
queries filter or sort on. Times are ISO-8601 UTC strings with microseconds, so
they sort as text.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

repos = Table(
    "repos",
    metadata,
    Column("repo", String, primary_key=True),
    Column("active", Boolean, nullable=False, default=False),
    Column("paused", Boolean, nullable=False, default=False),
    Column("data", Text, nullable=False),
)

profile_states = Table(
    "profile_states",
    metadata,
    Column("repo", String, primary_key=True),
    Column("data", Text, nullable=False),
)

pr_snapshots = Table(
    "pr_snapshots",
    metadata,
    Column("repo", String, primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("state", String, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

evaluations = Table(
    "evaluations",
    metadata,
    Column("repo", String, primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("tier", String, nullable=False),
    Column("total", String, nullable=False),
    Column("data", Text, nullable=False),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("playbook", String, nullable=False),
    Column("key", String, nullable=False),
    Column("status", String, nullable=False),
    Column("pool", String, nullable=False),
    Column("repo", String),
    Column("number", Integer),
    Column("lease_until", String),
    Column("not_before", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("data", Text, nullable=False),
    UniqueConstraint("playbook", "key", name="uq_jobs_playbook_key"),
)
Index("ix_jobs_status_pool", jobs.c.status, jobs.c.pool)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("job_id", String),
    Column("repo", String),
    Column("runtime", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_agent_runs_runtime_created", agent_runs.c.runtime, agent_runs.c.created_at)

actions = Table(
    "actions",
    metadata,
    Column("id", String, primary_key=True),
    Column("idem_key", String, nullable=False, unique=True),
    Column("repo", String, nullable=False),
    Column("number", Integer),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_actions_repo_number", actions.c.repo, actions.c.number)
Index("ix_actions_status", actions.c.status)

proposals = Table(
    "proposals",
    metadata,
    Column("id", String, primary_key=True),
    Column("action_id", String, nullable=False),
    Column("repo", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_proposals_state", proposals.c.state)

agent_questions = Table(
    "agent_questions",
    metadata,
    Column("id", String, primary_key=True),
    Column("repo", String, nullable=True),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_agent_questions_state", agent_questions.c.state)

signals = Table(
    "signals",
    metadata,
    Column("id", String, primary_key=True),
    Column("repo", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("consumed", Boolean, nullable=False, default=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

review_records = Table(
    "review_records",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("repo", String, nullable=False),
    Column("number", Integer, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_review_records_pr", review_records.c.repo, review_records.c.number)

ci_observations = Table(
    "ci_observations",
    metadata,
    Column("id", String, primary_key=True),
    Column("repo", String, nullable=False),
    Column("sha", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

leases = Table(
    "leases",
    metadata,
    Column("lab", String, primary_key=True),
    Column("resource", String, primary_key=True),
    Column("holder", String, nullable=False),
    Column("expires_at", String, nullable=False),
)

merge_arms = Table(
    "merge_arms",
    metadata,
    Column("repo", String, primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("data", Text, nullable=False),
)

claims = Table(
    "claims",
    metadata,
    Column("repo", String, primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("login", String, nullable=False),
)

memory_changes = Table(
    "memory_changes",
    metadata,
    Column("id", String, primary_key=True),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

chat_threads = Table(
    "chat_threads",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

chat_messages = Table(
    "chat_messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("thread_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_chat_messages_thread", chat_messages.c.thread_id, chat_messages.c.created_at)

users = Table(
    "users",
    metadata,
    Column("login", String, primary_key=True),
    Column("data", Text, nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", String, primary_key=True),
    Column("repo", String),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)
Index("ix_audit_created", audit_log.c.created_at)

service_state = Table(
    "service_state",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
)
