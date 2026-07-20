"""Add users.locale column for the preferred UI language (i18n)

Revision ID: 2.0.14
Revises: 2.0.13
Create Date: 2026-07-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2.0.14'
down_revision = '2.0.13'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('locale', sa.String(length=5), nullable=True))


def downgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('locale')
