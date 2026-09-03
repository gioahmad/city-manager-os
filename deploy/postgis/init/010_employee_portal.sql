ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS submitted_by text,
    ADD COLUMN IF NOT EXISTS submitted_department text,
    ADD COLUMN IF NOT EXISTS employee_location text;

CREATE TABLE IF NOT EXISTS issue_checklist_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    label text NOT NULL,
    completed boolean NOT NULL DEFAULT false,
    completed_by text,
    completed_at timestamptz,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_issue_checklist_issue
    ON issue_checklist_items(issue_id, sort_order);

CREATE TABLE IF NOT EXISTS issue_updates (
    id bigserial PRIMARY KEY,
    issue_id uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    author text NOT NULL,
    note text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_issue_updates_issue
    ON issue_updates(issue_id, created_at DESC);
