ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS visibility_status text NOT NULL DEFAULT 'NONE',
    ADD COLUMN IF NOT EXISTS visibility_audience text,
    ADD COLUMN IF NOT EXISTS visibility_note text;

CREATE INDEX IF NOT EXISTS idx_issues_visibility
    ON issues(visibility_status, status);
