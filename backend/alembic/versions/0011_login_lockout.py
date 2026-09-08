"""Phase 12 — brute-force lockout columns (audit M11).

NON-DESTRUCTIVE by design (Phase 12 §61):
- only ADDS two nullable/defaulted columns to `users`
- existing rows keep working unchanged (server_default keeps NOT NULL safe)
- downgrade removes ONLY these two columns; no data is touched otherwise

Revision ID: 0011_login_lockout
Revises: 0010_team_admin_enterprise
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_login_lockout"
down_revision = "0010_team_admin_enterprise"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")
