BEGIN;

CREATE TABLE IF NOT EXISTS contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id text UNIQUE NOT NULL,
  name text NOT NULL,
  contact_type text NOT NULL DEFAULT 'OTHER',
  organization text,
  title text,
  phones text[] NOT NULL DEFAULT ARRAY[]::text[],
  emails text[] NOT NULL DEFAULT ARRAY[]::text[],
  address text,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  notes text,
  visibility text NOT NULL DEFAULT 'ALL',
  owner_username text,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (contact_type IN ('RESIDENT','STAFF','VENDOR','OFFICIAL','OTHER')),
  CHECK (visibility IN ('ALL','EXECUTIVE','PRIVATE'))
);

CREATE INDEX IF NOT EXISTS contacts_name_idx ON contacts(lower(name));
CREATE INDEX IF NOT EXISTS contacts_tags_gin ON contacts USING gin(tags);

ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS contact_id uuid REFERENCES contacts(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS subscribers_contact_id_idx ON subscribers(contact_id);

INSERT INTO contacts(contact_id,name,contact_type,tags,notes,visibility,owner_username,active)
SELECT 'RECIPIENT_' || s.subscriber_id,s.name,'OTHER',ARRAY['RECIPIENT']::text[],s.notes,'ALL','migration',s.active
FROM subscribers s
WHERE s.contact_id IS NULL
ON CONFLICT(contact_id) DO NOTHING;

UPDATE subscribers s
SET contact_id=c.id
FROM contacts c
WHERE s.contact_id IS NULL
  AND c.contact_id='RECIPIENT_' || s.subscriber_id;

CREATE OR REPLACE FUNCTION sync_subscriber_contact()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.contact_id IS NULL THEN
    INSERT INTO contacts(contact_id,name,contact_type,tags,notes,visibility,owner_username,active)
    VALUES('RECIPIENT_' || NEW.subscriber_id,NEW.name,'OTHER',ARRAY['RECIPIENT']::text[],NEW.notes,'ALL','recipient-sync',true)
    ON CONFLICT(contact_id) DO UPDATE SET name=EXCLUDED.name,updated_at=now()
    RETURNING id INTO NEW.contact_id;
  ELSIF TG_OP='UPDATE' AND NEW.name IS DISTINCT FROM OLD.name THEN
    UPDATE contacts SET name=NEW.name,updated_at=now() WHERE id=NEW.contact_id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS subscribers_contact_sync ON subscribers;
CREATE TRIGGER subscribers_contact_sync
BEFORE INSERT OR UPDATE OF name,contact_id ON subscribers
FOR EACH ROW EXECUTE FUNCTION sync_subscriber_contact();

CREATE TABLE IF NOT EXISTS issue_contacts (
  issue_id uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  contact_id uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  relationship text,
  is_primary boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(issue_id,contact_id)
);

CREATE TABLE IF NOT EXISTS contact_activity (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  activity_type text NOT NULL,
  summary text NOT NULL,
  actor text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS contact_activity_contact_idx
  ON contact_activity(contact_id,created_at DESC);

GRANT SELECT,INSERT,UPDATE,DELETE ON contacts,issue_contacts,contact_activity TO citymanager_app;

COMMIT;
