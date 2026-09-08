BEGIN;

CREATE TABLE IF NOT EXISTS executive_review_state (
    username text PRIMARY KEY,
    last_reviewed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_issues_quick_capture_inbox
ON issues (source,item_type,status,created_at DESC);

GRANT SELECT,INSERT,UPDATE,DELETE ON executive_review_state TO citymanager_app;

COMMIT;
