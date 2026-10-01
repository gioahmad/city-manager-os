BEGIN;
CREATE TABLE IF NOT EXISTS workspace_calendar_auth (
  state_hash text PRIMARY KEY, owner_username text NOT NULL, session_hash text NOT NULL,
  verifier text NOT NULL, expires_at timestamptz NOT NULL DEFAULT now()+interval '10 minutes'
);
CREATE TABLE IF NOT EXISTS workspace_calendar_connections (
  owner_username text PRIMARY KEY, tokens text NOT NULL, connected_at timestamptz NOT NULL DEFAULT now(),
  last_sync_at timestamptz, sync_error boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS workspace_calendar_events (
  owner_username text NOT NULL REFERENCES workspace_calendar_connections(owner_username) ON DELETE CASCADE,
  event_key text NOT NULL, title text NOT NULL, starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
  location text, all_day boolean NOT NULL DEFAULT false, outlook_url text,
  PRIMARY KEY(owner_username,event_key)
);
CREATE INDEX IF NOT EXISTS workspace_calendar_events_owner_date ON workspace_calendar_events(owner_username,starts_at);
GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_calendar_auth,workspace_calendar_connections,workspace_calendar_events TO citymanager_app;
COMMIT;
