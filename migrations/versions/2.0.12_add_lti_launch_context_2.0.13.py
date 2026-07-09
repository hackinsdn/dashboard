"""Add lti_launch_context table: AGS grade-passback context captured at
LTI launch time (one row per user + LMS activity)

Revision ID: 2.0.13
Revises: 2.0.12
Create Date: 2026-07-07 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2.0.13'
down_revision = '2.0.12'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'lti_launch_context',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('issuer', sa.String(length=255), nullable=False),
        sa.Column('client_id', sa.String(length=255), nullable=False),
        sa.Column('deployment_id', sa.String(length=255), nullable=True),
        sa.Column('resource_link_id', sa.String(length=255), nullable=True),
        sa.Column('ags', sa.Text(), nullable=True),
        sa.Column('custom_next_url', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.UniqueConstraint(
            'user_id', 'issuer', 'client_id', 'resource_link_id',
            name='uq_lti_launch_context',
        ),
    )
    op.create_index(
        op.f('ix_lti_launch_context_user_id'), 'lti_launch_context',
        ['user_id'], unique=False,
    )


def downgrade():
    op.drop_index(op.f('ix_lti_launch_context_user_id'), table_name='lti_launch_context')
    op.drop_table('lti_launch_context')
