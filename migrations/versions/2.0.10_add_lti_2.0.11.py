"""Add LTI 1.3 tables: lti_config (platform registrations keyed by issuer)
and lti_registration_tokens (one-time dynamic registration credentials)

Revision ID: 2.0.11
Revises: 2.0.10
Create Date: 2026-07-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2.0.11'
down_revision = '2.0.10'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'lti_config',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('issuer', sa.String(length=255), nullable=False),
        sa.Column('config', sa.Text(), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
    )
    op.create_index(op.f('ix_lti_config_issuer'), 'lti_config', ['issuer'], unique=True)

    op.create_table(
        'lti_registration_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )


def downgrade():
    op.drop_table('lti_registration_tokens')
    op.drop_index(op.f('ix_lti_config_issuer'), table_name='lti_config')
    op.drop_table('lti_config')
