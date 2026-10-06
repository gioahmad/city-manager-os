BEGIN;
-- Private references to externally stored event materials. No file bytes are stored here.
CREATE TABLE IF NOT EXISTS workspace_event_materials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_username text NOT NULL,
  source_kind text NOT NULL CHECK(source_kind IN ('EVENT','CALENDAR')),
  source_id uuid NOT NULL,
  provider text NOT NULL DEFAULT 'GOOGLE_DRIVE' CHECK(provider='GOOGLE_DRIVE'),
  external_id text,
  title text NOT NULL,
  url text NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_username,source_kind,source_id,url)
);
CREATE INDEX IF NOT EXISTS workspace_event_materials_source
  ON workspace_event_materials(owner_username,source_kind,source_id,created_at DESC);
GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_event_materials TO citymanager_app;
COMMIT;
