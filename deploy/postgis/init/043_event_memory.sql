BEGIN;
-- References and receipts only. No PDF/image bytes or extracted source text in this table.
CREATE TABLE IF NOT EXISTS event_memory_materials (
  id uuid PRIMARY KEY,
  owner_username text NOT NULL,
  source_kind text NOT NULL CHECK (source_kind IN ('EVENT','CALENDAR')),
  source_id uuid NOT NULL,
  visibility text NOT NULL DEFAULT 'PRIVATE' CHECK (visibility='PRIVATE'),
  descriptor jsonb NOT NULL CHECK (octet_length(descriptor::text) <= 4096),
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','READY')),
  vault_name text,
  note_path text,
  brain_note_id uuid REFERENCES brain_notes(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  stored_at timestamptz,
  CHECK (state <> 'READY' OR (stored_at IS NOT NULL AND note_path IS NOT NULL AND vault_name IS NOT NULL AND brain_note_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS event_memory_owner_source
  ON event_memory_materials(owner_username,source_kind,source_id,created_at DESC);
GRANT SELECT,INSERT,UPDATE,DELETE ON event_memory_materials TO citymanager_app;
COMMIT;
