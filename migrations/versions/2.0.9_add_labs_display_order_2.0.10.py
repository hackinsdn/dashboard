"""Added display_order column to labs for explicit listing order

Revision ID: 2.0.10
Revises: 2.0.9
Create Date: 2026-07-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2.0.10'
down_revision = '2.0.9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('labs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('display_order', sa.Integer(), nullable=False, server_default='1000'))
    # existing labs get the default position explicitly (the server_default
    # already covers engines that backfill on ADD COLUMN, e.g. SQLite)
    op.execute("UPDATE labs SET display_order = 1000 WHERE display_order IS NULL")


def downgrade():
    with op.batch_alter_table('labs', schema=None) as batch_op:
        batch_op.drop_column('display_order')
