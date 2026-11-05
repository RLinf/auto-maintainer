"""agent questions

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_questions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_questions", schema=None) as batch_op:
        batch_op.create_index("ix_agent_questions_state", ["state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_questions", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_questions_state")
    op.drop_table("agent_questions")
