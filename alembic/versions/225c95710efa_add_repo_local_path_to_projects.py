"""add repo_local_path to projects

Revision ID: 225c95710efa
Revises: add_user_preferred_model
Create Date: 2026-05-27 11:34:39.586505

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '225c95710efa'
down_revision: Union[str, Sequence[str], None] = 'add_user_preferred_model'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('projects', sa.Column('repo_local_path', sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('projects', 'repo_local_path')
