BEGIN;
-- Owner-private flags and receipts. No alert, Watch, routing or delivery rules change.
ALTER TABLE workspace_calendar_auth ADD COLUMN IF NOT EXISTS requested_scopes text NOT NULL DEFAULT '';
ALTER TABLE workspace_calendar_connections ADD COLUMN IF NOT EXISTS account_email text;
ALTER TABLE workspace_calendar_events ADD COLUMN IF NOT EXISTS calendar_key text NOT NULL DEFAULT 'primary';
ALTER TABLE workspace_calendar_events ADD COLUMN IF NOT EXISTS calendar_name text NOT NULL DEFAULT 'Primary calendar';
CREATE INDEX IF NOT EXISTS workspace_calendar_events_calendar_window
  ON workspace_calendar_events(owner_username,calendar_key,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS workspace_important (
  owner_username text NOT NULL, kind text NOT NULL, item_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_username,kind,item_id)
);
CREATE TABLE IF NOT EXISTS workspace_microsoft_calendars (
  owner_username text NOT NULL REFERENCES workspace_calendar_connections(owner_username) ON DELETE CASCADE,
  calendar_key text NOT NULL, name text NOT NULL, can_edit boolean NOT NULL DEFAULT false,
  last_sync_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_username,calendar_key)
);
CREATE TABLE IF NOT EXISTS workspace_capture_receipts (
  owner_username text NOT NULL, request_id uuid NOT NULL, fingerprint text NOT NULL,
  result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_username,request_id)
);
CREATE TABLE IF NOT EXISTS workspace_microsoft_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  request_id uuid NOT NULL, fingerprint text NOT NULL,
  operation text NOT NULL CHECK(operation IN ('MAIL_SEND','MAIL_DRAFT','CALENDAR_CREATE')),
  account_email text NOT NULL, source_kind text, source_id uuid,
  payload text NOT NULL, review jsonb NOT NULL,
  status text NOT NULL DEFAULT 'REVIEW' CHECK(status IN ('REVIEW','RUNNING','SUCCEEDED','FAILED','UNKNOWN','CANCELLED')),
  result jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes', updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_username,request_id)
);
CREATE INDEX IF NOT EXISTS workspace_microsoft_operations_owner
  ON workspace_microsoft_operations(owner_username,created_at DESC);
GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_important,workspace_microsoft_calendars,
  workspace_capture_receipts,workspace_microsoft_operations TO citymanager_app;
COMMIT;
