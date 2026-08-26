"""Initial inventory schema: applications and packages.

Creates the Phase 1 tables. The unique constraint on ``packages`` is what makes
``INSERT ... ON CONFLICT`` upserts possible and prevents duplicate package rows.

Revision ID: 0001
Revises:
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "content_guid",
            sa.String(length=64),
            nullable=False,
            comment="Posit Connect content GUID - stable across redeployments.",
        ),
        sa.Column("app_name", sa.String(length=512), nullable=False),
        sa.Column("owner", sa.String(length=256), nullable=True),
        sa.Column("content_url", sa.String(length=1024), nullable=True),
        sa.Column("bundle_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Connect created_time.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Connect last_deployed_time.",
        ),
        sa.Column(
            "last_inventory_scan",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this row was last refreshed by a scan.",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_guid", name="uq_applications_content_guid"),
        comment="Deployed Posit Connect content discovered by the inventory scan.",
    )
    op.create_index(
        "ix_applications_content_guid", "applications", ["content_guid"], unique=True
    )
    op.create_index("ix_applications_owner", "applications", ["owner"])
    op.create_index("ix_applications_updated_at", "applications", ["updated_at"])

    op.create_table(
        "packages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content_guid", sa.String(length=64), nullable=False),
        sa.Column("package_name", sa.String(length=256), nullable=False),
        sa.Column(
            "package_version",
            sa.String(length=128),
            server_default="",
            nullable=False,
            comment="Empty when unpinned.",
        ),
        sa.Column(
            "package_type",
            sa.String(length=32),
            nullable=False,
            comment="Python, R, Quarto, or Unknown.",
        ),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp of the scan that last saw this package.",
        ),
        sa.ForeignKeyConstraint(
            ["content_guid"],
            ["applications.content_guid"],
            name="fk_packages_content_guid",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_guid",
            "package_name",
            "package_version",
            "package_type",
            name="uq_packages_identity",
        ),
        comment="Package inventory per content item, one row per unique package.",
    )
    op.create_index("ix_packages_content_guid", "packages", ["content_guid"])
    op.create_index(
        "ix_packages_name_version", "packages", ["package_name", "package_version"]
    )
    op.create_index("ix_packages_type", "packages", ["package_type"])


def downgrade() -> None:
    op.drop_index("ix_packages_type", table_name="packages")
    op.drop_index("ix_packages_name_version", table_name="packages")
    op.drop_index("ix_packages_content_guid", table_name="packages")
    op.drop_table("packages")

    op.drop_index("ix_applications_updated_at", table_name="applications")
    op.drop_index("ix_applications_owner", table_name="applications")
    op.drop_index("ix_applications_content_guid", table_name="applications")
    op.drop_table("applications")
