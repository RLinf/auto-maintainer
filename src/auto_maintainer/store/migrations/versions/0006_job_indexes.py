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

"""Indexes on jobs by update time and playbook, so listing recent agent work skips background ticks.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16 11:40:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_jobs_updated_at", "jobs", ["updated_at"])
    op.create_index("ix_jobs_playbook_updated_at", "jobs", ["playbook", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_playbook_updated_at", table_name="jobs")
    op.drop_index("ix_jobs_updated_at", table_name="jobs")
