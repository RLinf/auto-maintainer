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

"""PR roles: each assignee is a PR's owner or one of its reviewers

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table(name: str, *, role: bool) -> None:
    columns = [
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
    ]
    if role:
        columns.append(sa.Column("role", sa.String(), nullable=False))
    op.create_table(
        name,
        *columns,
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("by", sa.String(), nullable=False),
        sa.Column("at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("repo", "number", "login", *(["role"] if role else [])),
    )


def upgrade() -> None:
    # Everyone assigned or claimed so far was asked to review: the dashboard's
    # Reviewers card made them.
    _table("pr_assignees_new", role=True)
    op.execute(
        "INSERT INTO pr_assignees_new (repo, number, login, role, kind, by, at) "
        "SELECT repo, number, login, 'reviewer', kind, by, at FROM pr_assignees"
    )
    op.drop_table("pr_assignees")
    op.rename_table("pr_assignees_new", "pr_assignees")


def downgrade() -> None:
    _table("pr_assignees_old", role=False)
    op.execute(
        "INSERT OR IGNORE INTO pr_assignees_old (repo, number, login, kind, by, at) "
        "SELECT repo, number, login, kind, by, at FROM pr_assignees "
        "ORDER BY role DESC"
    )
    op.drop_table("pr_assignees")
    op.rename_table("pr_assignees_old", "pr_assignees")
