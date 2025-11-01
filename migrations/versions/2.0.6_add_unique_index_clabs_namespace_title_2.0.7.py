"""add unique index on clabs(namespace_default, title)

Revision ID: 2.0.7
Revises: 2.0.6
Create Date: 2025-10-21
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2.0.7"
down_revision = "2.0.6"
branch_labels = None
depends_on = None


def upgrade():
    # Índice único composto
    op.create_index(
        "ix_clabs_namespace_title_unique",
        "clabs",
        ["namespace_default", "title"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_clabs_namespace_title_unique", table_name="clabs")
