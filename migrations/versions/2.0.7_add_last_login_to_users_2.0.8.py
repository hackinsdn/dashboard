"""add last_login column on users

Revision ID: 2.0.8
Revises: 2.0.7
Create Date: 2025-12-01
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2.0.8"
down_revision = "2.0.7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("last_login", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column("users", "last_login")
