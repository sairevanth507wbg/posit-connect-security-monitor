"""Add owner_email to applications.

The scan already fetches each owner's email from Connect but had nowhere to
store it. The OIS scanning handoff needs it so findings can be routed back to
the right person.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("owner_email", sa.String(length=320), nullable=True),
    )
    op.create_index("ix_applications_owner_email", "applications", ["owner_email"])


def downgrade() -> None:
    op.drop_index("ix_applications_owner_email", table_name="applications")
    op.drop_column("applications", "owner_email")
