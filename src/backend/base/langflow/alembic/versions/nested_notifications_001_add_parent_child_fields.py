"""Add parent-child relationship fields for nested notifications.

Description: EXPAND phase - Add optional fields to support nested progress notifications
Phase: EXPAND
Revision ID: nested_notifications_001
Revises: 63b9c451fd30
Create Date: 2026-04-20 15:30:00.000000

This migration adds three new optional fields to the message table to support
nested progress notifications:
- parent_message_id: Links child notifications to parent messages
- message_context: Identifies the relationship type
- tool_call_id: Associates notifications with specific tool invocations

All fields are nullable for backward compatibility (EXPAND phase).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from langflow.utils import migration

# revision identifiers, used by Alembic.
revision = "nested_notifications_001"  # pragma: allowlist secret
down_revision = "0e6138e7a0c2"  # pragma: allowlist secret  # Latest migration: add_ondelete_cascade_to_file_user_id_fk
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add parent-child relationship fields to message table (EXPAND phase)."""
    conn = op.get_bind()

    # Check if message table exists
    if not migration.table_exists("message", conn):
        return

    # Add parent_message_id column if it doesn't exist
    if not migration.column_exists("message", "parent_message_id", conn):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "parent_message_id",
                    postgresql.UUID(as_uuid=True) if conn.dialect.name == "postgresql" else sa.String(36),
                    nullable=True,
                )
            )

    # Add message_context column if it doesn't exist
    if not migration.column_exists("message", "message_context", conn):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("message_context", sa.String(100), nullable=True)
            )

    # Add tool_call_id column if it doesn't exist
    if not migration.column_exists("message", "tool_call_id", conn):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("tool_call_id", sa.String(100), nullable=True)
            )

    # Add foreign key constraint for parent_message_id (with existence check)
    if not migration.foreign_key_exists("message", "fk_message_parent_message_id", conn):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.create_foreign_key(
                "fk_message_parent_message_id",
                "message",
                ["parent_message_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # Add indexes for performance (use try-except to handle existing indexes)
    try:
        op.create_index(
            "ix_message_parent_message_id",
            "message",
            ["parent_message_id"],
        )
    except Exception:
        # Index already exists, skip
        pass

    try:
        op.create_index(
            "ix_message_tool_call_id",
            "message",
            ["tool_call_id"],
        )
    except Exception:
        # Index already exists, skip
        pass


def downgrade() -> None:
    """Remove parent-child relationship fields from message table."""
    conn = op.get_bind()

    # Check if message table exists
    if not migration.table_exists("message", conn):
        return

    # Remove indexes (use try-except to handle non-existent indexes)
    try:
        op.drop_index("ix_message_tool_call_id", table_name="message")
    except Exception:
        # Index doesn't exist, skip
        pass

    try:
        op.drop_index("ix_message_parent_message_id", table_name="message")
    except Exception:
        # Index doesn't exist, skip
        pass

    # Remove foreign key constraint (with existence check)
    if migration.foreign_key_exists("message", "fk_message_parent_message_id", conn):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.drop_constraint("fk_message_parent_message_id", type_="foreignkey")

    # Remove columns (with existence checks)
    with op.batch_alter_table("message", schema=None) as batch_op:
        if migration.column_exists("message", "tool_call_id", conn):
            batch_op.drop_column("tool_call_id")

        if migration.column_exists("message", "message_context", conn):
            batch_op.drop_column("message_context")

        if migration.column_exists("message", "parent_message_id", conn):
            batch_op.drop_column("parent_message_id")

# Made with Bob
