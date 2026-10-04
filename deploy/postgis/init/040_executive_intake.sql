BEGIN;

ALTER TABLE workspace_calendar_events
  ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS workspace_calendar_events_id_unique
  ON workspace_calendar_events(id);

GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_calendar_events TO citymanager_app;

COMMIT;
