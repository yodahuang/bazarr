"""Add bilingual subtitle metadata.

Revision ID: a7d3e9f14c2b
Revises: 0124f9e278fb
Create Date: 2026-09-13 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7d3e9f14c2b'
down_revision = '0124f9e278fb'
branch_labels = None
depends_on = None


bind = op.get_context().bind


def column_exists(table_name, column_name):
    return any(column['name'] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade():
    table_specs = (
        {
            'name': 'table_episodes_subtitles',
        },
        {
            'name': 'table_movies_subtitles',
        },
    )

    for spec in table_specs:
        table_name = spec['name']

        with op.batch_alter_table(table_name, schema=None) as batch_op:
            if not column_exists(table_name, 'content_type'):
                batch_op.add_column(
                    sa.Column('content_type', sa.Text(), nullable=False, server_default='single')
                )
            if not column_exists(table_name, 'secondary_language'):
                batch_op.add_column(sa.Column('secondary_language', sa.Text(), nullable=False, server_default=''))


def downgrade():
    pass
