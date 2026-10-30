"""Proof fixture: a revision whose downgrade undoes its upgrade."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the proof table."""
    op.create_table("proof_good_table", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    """Drop the proof table."""
    op.drop_table("proof_good_table")
