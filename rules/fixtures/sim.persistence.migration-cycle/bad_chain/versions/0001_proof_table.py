"""Proof fixture: a revision whose downgrade leaves its table behind.

Upgrading again after the downgrade fails, because the table still exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the proof table."""
    op.create_table("proof_bad_table", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    """Leave the proof table behind, which the cycle must catch."""
