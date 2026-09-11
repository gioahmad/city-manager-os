BEGIN;

CREATE TABLE IF NOT EXISTS gis_refresh_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id text NOT NULL UNIQUE,
  scope text NOT NULL DEFAULT 'NJ_STATEWIDE',
  mode text NOT NULL,
  status text NOT NULL CHECK (status IN ('RUNNING','SUCCESS','FAILED','UNCHANGED')),
  phase text NOT NULL,
  source_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
  progress jsonb NOT NULL DEFAULT '{}'::jsonb,
  repository_sha text,
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  error_message text
);

CREATE INDEX IF NOT EXISTS idx_gis_refresh_runs_started
  ON gis_refresh_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_gis_refresh_runs_status
  ON gis_refresh_runs(status,updated_at DESC);

GRANT SELECT,INSERT,UPDATE ON gis_refresh_runs TO citymanager_app;

COMMIT;
