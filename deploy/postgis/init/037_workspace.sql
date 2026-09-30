BEGIN;
-- Additive workspace storage; no alert, watch, recipient, or connector triggers change.
CREATE TABLE IF NOT EXISTS workspace_config (
  singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
  settings jsonb NOT NULL DEFAULT '{}'
);
INSERT INTO workspace_config(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS workspace_entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK(kind IN ('PERSON','ORGANIZATION','BUILDING','STREET','PROJECT','DOCUMENT')),
  name text NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
  visibility text NOT NULL CHECK(visibility IN ('PRIVATE','WORK')),
  owner_username text NOT NULL,
  attributes jsonb NOT NULL DEFAULT '{}',
  contact_id uuid UNIQUE REFERENCES contacts(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_entities_scope ON workspace_entities(visibility,owner_username);
CREATE TABLE IF NOT EXISTS workspace_relationships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES workspace_entities(id),
  target_id uuid NOT NULL REFERENCES workspace_entities(id),
  relation text NOT NULL,
  visibility text NOT NULL CHECK(visibility IN ('PRIVATE','WORK')),
  owner_username text NOT NULL,
  evidence text NOT NULL DEFAULT '',
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK(source_id<>target_id),
  UNIQUE(source_id,target_id,relation,owner_username)
);
CREATE INDEX IF NOT EXISTS workspace_relationships_scope ON workspace_relationships(visibility,owner_username);
CREATE TABLE IF NOT EXISTS workspace_dismissed (
  owner_username text NOT NULL, fingerprint text NOT NULL,
  PRIMARY KEY(owner_username,fingerprint)
);
CREATE TABLE IF NOT EXISTS workspace_dates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES workspace_entities(id),
  owner_username text NOT NULL,
  occasion text NOT NULL CHECK(occasion IN ('BIRTHDAY','ANNIVERSARY','REMEMBRANCE','FOLLOW_UP','OTHER')),
  label text NOT NULL,
  event_date date NOT NULL,
  annual boolean NOT NULL DEFAULT false,
  lead_days integer NOT NULL DEFAULT 0 CHECK(lead_days BETWEEN 0 AND 365),
  context text NOT NULL DEFAULT '',
  handled_occurrence date,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_dates_owner ON workspace_dates(owner_username,active);
CREATE TABLE IF NOT EXISTS workspace_personal_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  title text NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
  due_date date, done boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_personal_tasks_owner ON workspace_personal_tasks(owner_username,done);
CREATE TABLE IF NOT EXISTS workspace_health (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  kind text NOT NULL CHECK(kind IN ('WATER','PROTEIN')),
  amount numeric NOT NULL CHECK(amount>0 AND amount<=10000),
  logged_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_health_owner ON workspace_health(owner_username,logged_at);
CREATE TABLE IF NOT EXISTS workspace_fasts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_username text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
  CHECK(ended_at IS NULL OR ended_at>=started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_one_active_fast ON workspace_fasts(owner_username) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS workspace_goals (
  owner_username text PRIMARY KEY, water_ml numeric, protein_g numeric,
  CHECK(water_ml IS NULL OR water_ml BETWEEN 1 AND 10000),
  CHECK(protein_g IS NULL OR protein_g BETWEEN 1 AND 1000)
);
CREATE TABLE IF NOT EXISTS workspace_messages (
  id uuid PRIMARY KEY, owner_username text NOT NULL,
  entity_id uuid NOT NULL REFERENCES workspace_entities(id),
  date_id uuid REFERENCES workspace_dates(id), occurrence date,
  phone text NOT NULL, body text NOT NULL,
  status text NOT NULL CHECK(status IN ('PENDING','ACCEPTED','UNKNOWN')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspace_portals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), issue_id uuid NOT NULL REFERENCES issues(id),
  token_hash text UNIQUE NOT NULL, owner_username text NOT NULL,
  expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
  revoked boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspace_portal_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), portal_id uuid NOT NULL REFERENCES workspace_portals(id),
  author text NOT NULL CHECK(author IN ('REQUESTER','STAFF')), body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4000),
  created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE,DELETE ON workspace_config,workspace_entities,workspace_relationships,
  workspace_dismissed,workspace_dates,workspace_personal_tasks,workspace_health,workspace_fasts,
  workspace_goals,workspace_messages,workspace_portals,workspace_portal_messages TO citymanager_app;
COMMIT;
