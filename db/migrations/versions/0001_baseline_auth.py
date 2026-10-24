"""baseline auth schema: users, capabilities, user_capabilities

Revision ID: 0001_baseline_auth
Revises:
Create Date: 2026-10-23

A01 baseline migration. Mirrors services/api/app/models.py exactly. Later
tasks (A02+) add further migrations on top of this one -- never edit this
file once it has been applied anywhere (see TASK_IMPLEMENTATION.md git
discipline: "never rewrite a migration already applied elsewhere").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline_auth"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "capabilities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_capabilities_code", "capabilities", ["code"])
    op.create_index("ix_capabilities_code", "capabilities", ["code"])

    op.create_table(
        "user_capabilities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "capability_id",
            sa.String(length=32),
            sa.ForeignKey("capabilities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "capability_id", name="uq_user_capability"),
    )
    op.create_index("ix_user_capabilities_user_id", "user_capabilities", ["user_id"])
    op.create_index("ix_user_capabilities_capability_id", "user_capabilities", ["capability_id"])


def downgrade() -> None:
    op.drop_table("user_capabilities")
    op.drop_table("capabilities")
    op.drop_table("users")
