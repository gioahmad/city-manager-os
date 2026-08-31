ALTER TABLE issues
  ADD COLUMN IF NOT EXISTS operational_event_id uuid
  REFERENCES operational_events(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_issues_operational_event
  ON issues(operational_event_id);
