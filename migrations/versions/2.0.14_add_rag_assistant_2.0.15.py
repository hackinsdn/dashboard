"""Support chat: assistant mode, locale, message meta and answer feedback

Revision ID: 2.0.15
Revises: 2.0.14
Create Date: 2026-07-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2.0.15'
down_revision = '2.0.14'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('support_threads', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('mode', sa.String(length=16), nullable=False, server_default='support')
        )
        batch_op.add_column(sa.Column('locale', sa.String(length=8), nullable=True))
    # every pre-existing conversation is a human support case
    op.execute("UPDATE support_threads SET mode = 'support' WHERE mode IS NULL")

    with op.batch_alter_table('support_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('feedback', sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column('feedback_reason', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('feedback_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('support_messages', schema=None) as batch_op:
        batch_op.drop_column('feedback_at')
        batch_op.drop_column('feedback_reason')
        batch_op.drop_column('feedback')
        batch_op.drop_column('meta')

    with op.batch_alter_table('support_threads', schema=None) as batch_op:
        batch_op.drop_column('locale')
        batch_op.drop_column('mode')
