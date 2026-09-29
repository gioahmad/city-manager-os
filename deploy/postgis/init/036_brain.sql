BEGIN;
CREATE TABLE IF NOT EXISTS brain_notes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_username text NOT NULL,
  body text NOT NULL CHECK (length(body) BETWEEN 1 AND 20000),
  kind text NOT NULL DEFAULT 'NOTE' CHECK (kind IN ('NOTE','IDEA','TASK','LINK')),
  tags text[] NOT NULL DEFAULT '{}',
  source text NOT NULL DEFAULT 'WEB' CHECK (source IN ('WEB','SMS')),
  source_id text,
  pinned boolean NOT NULL DEFAULT false,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source,source_id)
);
CREATE INDEX IF NOT EXISTS brain_notes_owner_date ON brain_notes(owner_username,created_at DESC);
CREATE INDEX IF NOT EXISTS brain_notes_search ON brain_notes USING gin(to_tsvector('english',body));
CREATE TABLE IF NOT EXISTS brain_attachments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES brain_notes(id) ON DELETE CASCADE,
  filename text NOT NULL,
  content bytea NOT NULL CHECK (octet_length(content) <= 20971520)
);
CREATE INDEX IF NOT EXISTS brain_attachments_note ON brain_attachments(note_id);
CREATE TABLE IF NOT EXISTS brain_sms_settings (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  owner_username text NOT NULL,
  senders text[] NOT NULL,
  signing_key text NOT NULL
);
GRANT SELECT,INSERT,UPDATE,DELETE ON brain_notes,brain_attachments,brain_sms_settings TO citymanager_app;
COMMIT;
