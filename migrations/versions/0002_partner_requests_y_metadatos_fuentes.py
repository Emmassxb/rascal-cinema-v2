"""partner_requests y metadatos de fuentes (idioma de audio, calidad)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23

* ``media_sources``: columnas nullable ``audio_language`` y ``quality``. Las filas existentes
  quedan en NULL y la UI las muestra como "Sin especificar" (no se inventan valores).
* ``partner_requests``: solicitudes para pasar a ``partner``. Índice único parcial: como
  máximo UNA solicitud ``pending`` por usuario.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_sources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_language", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("quality", sa.String(length=20), nullable=True))

    op.create_table(
        "partner_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="partner_request_status",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("portfolio_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_partner_requests_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], name=op.f("fk_partner_requests_reviewed_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_requests")),
    )
    with op.batch_alter_table("partner_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_partner_requests_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_partner_requests_status"), ["status"], unique=False)
    op.create_index(
        "uq_partner_requests_one_pending",
        "partner_requests",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_partner_requests_one_pending", table_name="partner_requests")
    with op.batch_alter_table("partner_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_partner_requests_status"))
        batch_op.drop_index(batch_op.f("ix_partner_requests_user_id"))
    op.drop_table("partner_requests")

    with op.batch_alter_table("media_sources", schema=None) as batch_op:
        batch_op.drop_column("quality")
        batch_op.drop_column("audio_language")
