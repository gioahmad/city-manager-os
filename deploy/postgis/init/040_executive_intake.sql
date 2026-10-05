BEGIN;

ALTER TABLE workspace_calendar_events
  ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS workspace_calendar_events_id_unique
  ON workspace_calendar_events(id);

CREATE TABLE IF NOT EXISTS workspace_inbox_snoozed (
  owner_username text NOT NULL,
  kind text NOT NULL,
  item_id uuid NOT NULL,
  snoozed_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_username,kind,item_id)
);
CREATE INDEX IF NOT EXISTS workspace_inbox_snoozed_due
  ON workspace_inbox_snoozed(owner_username,snoozed_until);

GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_calendar_events,workspace_inbox_snoozed TO citymanager_app;

COMMIT;
