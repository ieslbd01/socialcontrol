"""initial schema (TDD-02)

Revision ID: 0001
Revises:
Rollback note: drops every SocialControl table; restore from backup if data exists.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
create table platforms (
  key text primary key,
  display_name text not null,
  code text not null unique,
  adapter text not null,
  default_mode text not null check (default_mode in ('AUTO','ASSISTED')),
  enabled boolean not null default true
);

create table platform_capabilities (
  platform_key text not null references platforms(key),
  post_type text not null,
  max_caption_chars int,
  max_hashtags int,
  requires_media boolean not null default false,
  media_types text[] not null default '{}',
  max_image_mb int,
  max_video_mb int,
  max_video_seconds int,
  aspect_ratios text[] not null default '{}',
  daily_post_cap int,
  extra jsonb not null default '{}',
  primary key (platform_key, post_type)
);

create table platform_accounts (
  id uuid primary key default gen_random_uuid(),
  platform_key text not null references platforms(key),
  short_name text not null,
  display_name text not null,
  external_id text,
  mode text not null check (mode in ('AUTO','ASSISTED')),
  state text not null default 'NOT_CONFIGURED'
    check (state in ('NOT_CONFIGURED','CONNECTED','DISCONNECTED','TOKEN_EXPIRED','ERROR','DISABLED')),
  test_mode boolean not null default false,
  destination_url text,
  credentials_enc bytea,
  token_expires_at timestamptz,
  last_publish_at timestamptz,
  last_error text,
  settings jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (platform_key, short_name)
);

create table content_items (
  content_id text primary key check (content_id ~ '^C[0-9]{1,6}$'),
  title text not null,
  notes text,
  website_url text,
  tags text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);

create table import_batches (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  csv_name text,
  zip_names text[],
  status text not null check (status in ('UPLOADED','DRY_RUN','CONFIRMING','CONFIRMED','CANCELLED','UNDONE','FAILED')),
  counts jsonb not null default '{}',
  report jsonb
);

create table media (
  id uuid primary key default gen_random_uuid(),
  sha256 text not null unique,
  filename text not null,
  mime text not null,
  bytes bigint not null check (bytes >= 0),
  width int,
  height int,
  duration_s numeric,
  backend text not null check (backend in ('supabase','r2','url')),
  storage_key text not null,
  public_url text,
  created_at timestamptz not null default now(),
  archived_at timestamptz
);

create table queues (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references platform_accounts(id),
  name text not null,
  is_default boolean not null default false,
  status text not null default 'ACTIVE' check (status in ('ACTIVE','PAUSED','COMPLETED','DISABLED')),
  timezone text not null default 'Asia/Dhaka',
  start_at timestamptz not null,
  recurrence jsonb not null,
  pattern jsonb not null default '[]',
  pattern_mode text not null default 'RELAXED' check (pattern_mode in ('STRICT','RELAXED')),
  pattern_pointer int not null default 0 check (pattern_pointer >= 0),
  priority int not null default 100,
  end_at timestamptz,
  max_posts int check (max_posts is null or max_posts > 0),
  skip_rules jsonb not null default '[]',
  require_approval boolean not null default true,
  keep_holes boolean not null default false,
  evergreen_enabled boolean not null default false,
  evergreen_gap_days int not null default 90 check (evergreen_gap_days >= 0),
  runway_threshold_days int not null default 14,
  min_gap_minutes int not null default 120,
  default_post_type text,
  horizon_days int not null default 90,
  row_version int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (account_id, name)
);
create unique index queues_one_default_per_account on queues (account_id) where is_default;

create table posts (
  post_id text primary key check (post_id ~ '^C[0-9]+-[A-Z]{2,3}(-[0-9]+)?(-R[0-9]+)?$'),
  content_id text not null references content_items(content_id),
  account_id uuid not null references platform_accounts(id),
  queue_id uuid references queues(id),
  post_type text not null,
  language text not null default 'en' check (language in ('en','bn','en+bn')),
  title text,
  caption text,
  link_url text,
  hashtags text[] not null default '{}',
  evergreen boolean not null default false,
  queue_position int,
  status text not null default 'DRAFT' check (status in (
    'DRAFT','IN_REVIEW','APPROVED','QUEUED','SCHEDULED','PUBLISHING','AWAITING_CONFIRMATION',
    'PUBLISHED','FAILED','RETRYING','FAILED_FINAL','OVERDUE','NEEDS_ATTENTION','SKIPPED','CANCELLED','ARCHIVED')),
  scheduled_at timestamptz,
  approved_at timestamptz,
  approved_hash text,
  locked_by text,
  locked_at timestamptz,
  attempt_count int not null default 0,
  next_retry_at timestamptz,
  platform_post_id text,
  published_url text,
  published_at timestamptz,
  confirmed_by_owner boolean not null default false,
  pinned boolean not null default false,
  source_post_id text references posts(post_id),
  source_batch uuid references import_batches(id),
  notes text,
  row_version int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);
create index posts_due_idx on posts (scheduled_at) where status in ('SCHEDULED','RETRYING');
create index posts_queue_idx on posts (queue_id, status, queue_position);
create index posts_content_idx on posts (content_id);

create table post_media (
  post_id text not null references posts(post_id) on delete cascade,
  media_id uuid not null references media(id),
  role text not null default 'primary',
  sort int not null default 0,
  primary key (post_id, media_id, role)
);

create table queue_slots (
  id uuid primary key default gen_random_uuid(),
  queue_id uuid not null references queues(id) on delete cascade,
  slot_at timestamptz not null,
  pattern_index int,
  post_id text references posts(post_id),
  state text not null default 'OPEN' check (state in ('OPEN','FILLED','EMPTY','SKIPPED','DONE')),
  filled_by text check (filled_by in ('FIFO','OVERRIDE','EVERGREEN')),
  unique (queue_id, slot_at),
  check ((state = 'FILLED') = (post_id is not null) or state in ('DONE','SKIPPED'))
);
create unique index queue_slots_post_uidx on queue_slots (post_id) where state = 'FILLED';

create table publish_attempts (
  id uuid primary key default gen_random_uuid(),
  post_id text not null references posts(post_id),
  slot_id uuid references queue_slots(id),
  run_id text,
  attempt_no int not null,
  attempt_type text not null check (attempt_type in ('AUTO_PUBLISH','ASSISTED_DELIVERY','ASSISTED_CONFIRM','MANUAL')),
  platform_key text not null,
  account_id uuid not null,
  queue_id uuid,
  scheduled_at timestamptz,
  started_at timestamptz not null,
  finished_at timestamptz,
  result text not null check (result in ('SUCCESS','FAILED','DELIVERED','CONFIRMED','SKIPPED')),
  failure_class text check (failure_class in ('TEMPORARY','RATE_LIMIT','AUTH','VALIDATION','PERMANENT_REJECTION','UNKNOWN')),
  error_code text,
  error_message text,
  platform_post_id text,
  published_url text,
  idempotency_key text,
  response_meta jsonb
);
create index attempts_post_idx on publish_attempts (post_id, started_at desc);

create table assisted_tasks (
  id uuid primary key default gen_random_uuid(),
  post_id text not null references posts(post_id),
  delivered_at timestamptz,
  token_hash text,
  token_expires_at timestamptz,
  reminders_sent int not null default 0,
  next_reminder_at timestamptz,
  state text not null default 'PENDING' check (state in ('PENDING','DELIVERED','CONFIRMED','SKIPPED','OVERDUE')),
  confirmed_at timestamptz,
  confirmed_url text
);

create table post_audit (
  id bigserial primary key,
  post_id text,
  at timestamptz not null default now(),
  actor text,
  action text,
  field text,
  old_value text,
  new_value text,
  reason text
);

create table notification_rules (
  event text primary key,
  channels text[] not null,
  enabled boolean not null default true,
  severity text not null
);
create table notification_logs (
  id bigserial primary key,
  at timestamptz not null default now(),
  event text,
  channel text,
  dedup_key text,
  ok boolean,
  detail text
);
create index notif_dedup_idx on notification_logs (dedup_key, at desc);

create table reports (
  id uuid primary key default gen_random_uuid(),
  kind text,
  period_start date,
  period_end date,
  created_at timestamptz not null default now(),
  files jsonb,
  summary jsonb
);
create table settings (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
create table job_runs (
  id uuid primary key default gen_random_uuid(),
  job text,
  run_id text,
  started_at timestamptz,
  finished_at timestamptz,
  ok boolean,
  summary jsonb
);
create table users_profile (
  id uuid primary key,
  role text not null default 'admin'
);
create table security_events (
  id bigserial primary key,
  at timestamptz not null default now(),
  kind text,
  detail jsonb
);

-- Integrity guards ---------------------------------------------------------
-- A post can only enter a publishing state if it has been approved (REV-01).
create function posts_status_guard() returns trigger language plpgsql as $$
begin
  if new.status in ('SCHEDULED','PUBLISHING','AWAITING_CONFIRMATION','PUBLISHED')
     and new.approved_at is null then
    raise exception 'post % cannot be % without approval', new.post_id, new.status
      using errcode = 'check_violation';
  end if;
  return new;
end $$;
create trigger posts_status_guard_trg before insert or update on posts
  for each row execute function posts_status_guard();

-- Published content is immutable (CNT-06).
create function posts_published_immutable() returns trigger language plpgsql as $$
begin
  if old.status = 'PUBLISHED' and (
       new.caption is distinct from old.caption or new.title is distinct from old.title
    or new.link_url is distinct from old.link_url or new.post_type is distinct from old.post_type
    or new.hashtags is distinct from old.hashtags or new.account_id is distinct from old.account_id) then
    raise exception 'published post % is read-only', old.post_id using errcode = 'check_violation';
  end if;
  return new;
end $$;
create trigger posts_published_immutable_trg before update on posts
  for each row execute function posts_published_immutable();

-- Keep updated_at fresh.
create function touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
create trigger posts_touch before update on posts for each row execute function touch_updated_at();
create trigger queues_touch before update on queues for each row execute function touch_updated_at();
create trigger accounts_touch before update on platform_accounts for each row execute function touch_updated_at();
"""

TABLES = [
    "security_events",
    "users_profile",
    "job_runs",
    "settings",
    "reports",
    "notification_logs",
    "notification_rules",
    "post_audit",
    "assisted_tasks",
    "publish_attempts",
    "queue_slots",
    "post_media",
    "posts",
    "queues",
    "media",
    "import_batches",
    "content_items",
    "platform_accounts",
    "platform_capabilities",
    "platforms",
]


def upgrade() -> None:
    # exec_driver_sql avoids ':' / '%' parameter parsing inside the DDL (regexes, $$ bodies)
    op.get_bind().exec_driver_sql(SCHEMA.replace("%", "%%"))


def downgrade() -> None:
    for fn in ("posts_status_guard", "posts_published_immutable", "touch_updated_at"):
        op.execute(f"drop function if exists {fn}() cascade")
    for table in TABLES:
        op.execute(f"drop table if exists {table} cascade")
