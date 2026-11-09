"""PR assignees: any number of reviewers per PR, claimed or assigned

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 10:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pr_assignees",
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("by", sa.String(), nullable=False),
        sa.Column("at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("repo", "number", "login"),
    )
    # Each PR's one claim becomes its first claimed reviewer.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    op.execute(
        sa.text(
            "INSERT INTO pr_assignees (repo, number, login, kind, by, at) "
            "SELECT repo, number, login, 'claimed', login, :now FROM claims"
        ).bindparams(now=now)
    )
    op.drop_table("claims")


def downgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("repo", "number"),
    )
    op.execute(
        "INSERT INTO claims (repo, number, login) "
        "SELECT repo, number, MIN(login) FROM pr_assignees GROUP BY repo, number"
    )
    op.drop_table("pr_assignees")
