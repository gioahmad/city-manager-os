BEGIN;
-- Independent, owner-private intake. Existing contacts, watches and delivery rules are untouched.
ALTER TABLE workspace_calendar_connections ADD COLUMN IF NOT EXISTS scopes text NOT NULL
  DEFAULT 'offline_access https://graph.microsoft.com/Calendars.ReadBasic';
CREATE TABLE IF NOT EXISTS workspace_microsoft_mail (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_username text NOT NULL REFERENCES workspace_calendar_connections(owner_username) ON DELETE CASCADE,
  provider_key text NOT NULL, title text NOT NULL, body text NOT NULL,
  sender_name text NOT NULL DEFAULT '', sender_email text NOT NULL DEFAULT '',
  recipients jsonb NOT NULL DEFAULT '[]', conversation_key text NOT NULL DEFAULT '',
  received_at timestamptz NOT NULL, is_read boolean NOT NULL DEFAULT false, outlook_url text,
  UNIQUE(owner_username,provider_key)
);
CREATE INDEX IF NOT EXISTS workspace_microsoft_mail_owner ON workspace_microsoft_mail(owner_username,received_at DESC);
CREATE TABLE IF NOT EXISTS workspace_microsoft_contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_username text NOT NULL REFERENCES workspace_calendar_connections(owner_username) ON DELETE CASCADE,
  provider_key text NOT NULL, name text NOT NULL, attributes jsonb NOT NULL DEFAULT '{}',
  imported_entity_id uuid REFERENCES workspace_entities(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(owner_username,provider_key)
);
CREATE TABLE IF NOT EXISTS workspace_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  filename text NOT NULL, content_type text NOT NULL, content bytea NOT NULL,
  status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','PROCESSING','READY','FAILED','NEEDS_OCR')),
  extracted_text text NOT NULL DEFAULT '', profile jsonb NOT NULL DEFAULT '{}',
  error text, attempts integer NOT NULL DEFAULT 0, started_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK(octet_length(content) BETWEEN 1 AND 20971520)
);
CREATE INDEX IF NOT EXISTS workspace_documents_owner ON workspace_documents(owner_username,created_at DESC);
CREATE INDEX IF NOT EXISTS workspace_documents_queue ON workspace_documents(created_at) WHERE status='QUEUED';
CREATE INDEX IF NOT EXISTS workspace_documents_search ON workspace_documents USING gin(to_tsvector('english',extracted_text));
CREATE TABLE IF NOT EXISTS workspace_inbox_handled (
  owner_username text NOT NULL, kind text NOT NULL, item_id uuid NOT NULL,
  handled_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(owner_username,kind,item_id)
);
CREATE TABLE IF NOT EXISTS workspace_context_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  source_kind text NOT NULL, source_id uuid NOT NULL, target_kind text NOT NULL, target_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK(source_kind<>target_kind OR source_id<>target_id),
  UNIQUE(owner_username,source_kind,source_id,target_kind,target_id)
);
GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_microsoft_mail,workspace_microsoft_contacts,
  workspace_documents,workspace_inbox_handled,workspace_context_links TO citymanager_app;
COMMIT;
