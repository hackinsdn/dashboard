"""add last_login column on users

Revision ID: 2.0.4
Revises: 2.0.3
Create Date: 2025-12-01
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2.0.4"
down_revision = "2.0.3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("last_login", sa.DateTime(), nullable=True),
    )

    connection = op.get_bind()

    result = connection.execute(sa.text("""
        SELECT u.id AS user_id, MAX(l.datetime) AS last_login
        FROM users u
        JOIN login_logging l
          ON l.login = u.username OR l.login = u.email
        GROUP BY u.id
    """))

    for user_id, last_login in result:
        connection.execute(
            sa.text("UPDATE users SET last_login = :last_login WHERE id = :user_id"),
            {"user_id": user_id, "last_login": last_login},
        )

def downgrade():
    op.drop_column("users", "last_login")
