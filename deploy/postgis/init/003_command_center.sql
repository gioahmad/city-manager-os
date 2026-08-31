ALTER TABLE issues
  ADD COLUMN IF NOT EXISTS item_type text NOT NULL DEFAULT 'ISSUE',
  ADD COLUMN IF NOT EXISTS next_action text,
  ADD COLUMN IF NOT EXISTS waiting_on text,
  ADD COLUMN IF NOT EXISTS due_at timestamptz,
  ADD COLUMN IF NOT EXISTS follow_up_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_issues_open_loop_dates
  ON issues(status, due_at, follow_up_at);
