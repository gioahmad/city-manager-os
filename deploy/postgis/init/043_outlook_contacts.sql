BEGIN;
CREATE TABLE IF NOT EXISTS outlook_contact_links (
  owner_username text NOT NULL, account_email text NOT NULL, provider_key text NOT NULL,
  contact_id uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  local_baseline jsonb NOT NULL, remote_baseline jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_username,account_email,provider_key),
  UNIQUE(owner_username,account_email,contact_id)
);
-- Links survive disconnects, but are only used with the same verified mailbox.
GRANT SELECT,INSERT,UPDATE,DELETE ON outlook_contact_links TO citymanager_app;
ALTER TABLE workspace_microsoft_operations DROP CONSTRAINT IF EXISTS workspace_microsoft_operations_operation_check;
ALTER TABLE workspace_microsoft_operations ADD CONSTRAINT workspace_microsoft_operations_operation_check
  CHECK(operation IN ('MAIL_SEND','MAIL_DRAFT','CALENDAR_CREATE','CONTACT_UPDATE'));
COMMIT;
