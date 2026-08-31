ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS decision_options text,
    ADD COLUMN IF NOT EXISTS recommendation text,
    ADD COLUMN IF NOT EXISTS decision_by timestamptz,
    ADD COLUMN IF NOT EXISTS decision_outcome text;

CREATE INDEX IF NOT EXISTS idx_issues_decision_by
    ON issues(item_type, status, decision_by);
