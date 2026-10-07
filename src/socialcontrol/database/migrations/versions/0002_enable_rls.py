"""enable row level security on every table (TDD-02 section 5, SEC-11)

Revision ID: 0002
Revises: 0001
Rollback note: only turns RLS off again; no data is touched.

RLS is switched on with **no policies**: anyone reaching the database through Supabase's
Data API (anon / authenticated roles) sees nothing. The application connects as the table
owner (``postgres`` on Supabase, which has BYPASSRLS), so it is unaffected.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = [
    "platforms",
    "platform_capabilities",
    "platform_accounts",
    "content_items",
    "import_batches",
    "media",
    "queues",
    "posts",
    "post_media",
    "queue_slots",
    "publish_attempts",
    "assisted_tasks",
    "post_audit",
    "notification_rules",
    "notification_logs",
    "reports",
    "settings",
    "job_runs",
    "users_profile",
    "security_events",
]


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"alter table {table} enable row level security")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"alter table {table} disable row level security")
