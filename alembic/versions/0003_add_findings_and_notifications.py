"""Add findings and notifications.

Phase 2 stores what the scanner reported and remembers who has already been
told. Wiz issues no stable finding id across scans, so identity is the tuple
(content_guid, package, version, vulnerability_id) that OIS confirmed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content_guid", sa.String(length=64), nullable=False),
        sa.Column("package_name", sa.String(length=256), nullable=False),
        sa.Column("package_version", sa.String(length=128), server_default="", nullable=False),
        sa.Column("package_type", sa.String(length=32), nullable=True),
        sa.Column("vulnerability_id", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("fixed_version", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.String(length=1024), nullable=True),
        sa.Column("source", sa.String(length=32), server_default="wiz", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["content_guid"], ["applications.content_guid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_guid", "package_name", "package_version", "vulnerability_id",
            name="uq_findings_identity",
        ),
    )
    op.create_index("ix_findings_content_guid", "findings", ["content_guid"])
    op.create_index("ix_findings_severity", "findings", ["severity"])
    op.create_index("ix_findings_vulnerability", "findings", ["vulnerability_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("content_guid", sa.String(length=64), nullable=False),
        sa.Column("package_name", sa.String(length=256), nullable=False),
        sa.Column("package_version", sa.String(length=128), server_default="", nullable=False),
        sa.Column("vulnerability_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="sent", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_email", "content_guid", "package_name", "package_version",
            "vulnerability_id",
            name="uq_notifications_identity",
        ),
    )
    op.create_index("ix_notifications_owner_email", "notifications", ["owner_email"])
    op.create_index("ix_notifications_content_guid", "notifications", ["content_guid"])
    op.create_index("ix_notifications_sent_at", "notifications", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_sent_at", table_name="notifications")
    op.drop_index("ix_notifications_content_guid", table_name="notifications")
    op.drop_index("ix_notifications_owner_email", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_findings_vulnerability", table_name="findings")
    op.drop_index("ix_findings_severity", table_name="findings")
    op.drop_index("ix_findings_content_guid", table_name="findings")
    op.drop_table("findings")
