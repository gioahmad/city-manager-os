CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;

-- Front-end managed external source registry. Secrets are NEVER stored here;
-- auth_config stores only environment-variable names and non-secret metadata.
CREATE TABLE IF NOT EXISTS integrations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  integration_key text UNIQUE NOT NULL,
  name text NOT NULL,
  active boolean NOT NULL DEFAULT false,
  category text NOT NULL DEFAULT 'GENERIC',
  adapter_type text NOT NULL DEFAULT 'HTTP',
  endpoint_url text NOT NULL,
  method text NOT NULL DEFAULT 'GET',
  auth_type text NOT NULL DEFAULT 'NONE',
  auth_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  request_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
  request_query jsonb NOT NULL DEFAULT '{}'::jsonb,
  request_body text,
  parser_kind text NOT NULL DEFAULT 'NONE',
  parser_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  poll_seconds integer NOT NULL DEFAULT 900 CHECK (poll_seconds BETWEEN 60 AND 86400),
  timeout_seconds integer NOT NULL DEFAULT 15 CHECK (timeout_seconds BETWEEN 1 AND 60),
  max_response_bytes integer NOT NULL DEFAULT 1000000 CHECK (max_response_bytes BETWEEN 1024 AND 5000000),
  allow_redirects boolean NOT NULL DEFAULT true,
  verify_tls boolean NOT NULL DEFAULT true,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (method IN ('GET','POST','PUT','PATCH','DELETE','HEAD')),
  CHECK (auth_type IN ('NONE','BEARER_ENV','BASIC_ENV','API_KEY_HEADER_ENV','API_KEY_QUERY_ENV')),
  CHECK (parser_kind IN ('NONE','JSON_EVENTS','RSS_EVENTS','ATOM_EVENTS','ICS_EVENTS'))
);

CREATE INDEX IF NOT EXISTS idx_integrations_active_category
  ON integrations(active, category, integration_key);

CREATE TABLE IF NOT EXISTS integration_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  integration_id uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
  run_type text NOT NULL DEFAULT 'POLL',
  status text NOT NULL,
  http_status integer,
  elapsed_ms integer,
  response_bytes integer,
  content_type text,
  items_found integer,
  items_changed integer,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  CHECK (run_type IN ('TEST','POLL','MANUAL'))
);

CREATE INDEX IF NOT EXISTS idx_integration_runs_recent
  ON integration_runs(integration_id, started_at DESC);

-- Canonical external-event intelligence. This is awareness/watch data, NOT a task table.
CREATE TABLE IF NOT EXISTS event_intelligence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_integration_id uuid REFERENCES integrations(id) ON DELETE SET NULL,
  source_event_key text UNIQUE NOT NULL,
  external_key text,
  fingerprint text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  title text NOT NULL,
  description text,
  event_type text NOT NULL DEFAULT 'EVENT',
  venue text,
  address text,
  municipality text,
  county text,
  state text,
  starts_at timestamptz,
  ends_at timestamptz,
  status text NOT NULL DEFAULT 'SCHEDULED',
  source_name text,
  source_url text,
  attendance_estimate integer,
  road_impact text,
  transit_impact text,
  affected_assets text[] NOT NULL DEFAULT '{}',
  impact_score integer NOT NULL DEFAULT 0 CHECK (impact_score BETWEEN 0 AND 100),
  impact_level text NOT NULL DEFAULT 'AWARENESS',
  impact_summary text,
  latitude double precision,
  longitude double precision,
  geom geometry(Point,4326),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  change_hash text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  last_changed_at timestamptz NOT NULL DEFAULT now(),
  alert_pending boolean NOT NULL DEFAULT false,
  alert_emitted_at timestamptz,
  promoted_event_id uuid REFERENCES operational_events(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (impact_level IN ('AWARENESS','WATCH','ALERT'))
);

CREATE INDEX IF NOT EXISTS idx_event_intel_time
  ON event_intelligence(active, starts_at);
CREATE INDEX IF NOT EXISTS idx_event_intel_impact
  ON event_intelligence(active, impact_level, impact_score DESC);
CREATE INDEX IF NOT EXISTS idx_event_intel_alert_pending
  ON event_intelligence(alert_pending, updated_at)
  WHERE alert_pending = true;
CREATE INDEX IF NOT EXISTS idx_event_intel_geom
  ON event_intelligence USING GIST (geom);

-- Extend the existing managed operational_events record instead of creating a second task/event system.
ALTER TABLE operational_events
  ADD COLUMN IF NOT EXISTS owner text,
  ADD COLUMN IF NOT EXISTS event_status text NOT NULL DEFAULT 'PLANNING',
  ADD COLUMN IF NOT EXISTS event_scope text NOT NULL DEFAULT 'MANAGED',
  ADD COLUMN IF NOT EXISTS source_url text,
  ADD COLUMN IF NOT EXISTS expected_attendance integer,
  ADD COLUMN IF NOT EXISTS impact_notes text,
  ADD COLUMN IF NOT EXISTS event_intelligence_id uuid REFERENCES event_intelligence(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_operational_events_scope_status
  ON operational_events(event_scope, event_status, starts_at);

ALTER TABLE issues
  ADD COLUMN IF NOT EXISTS event_intelligence_id uuid REFERENCES event_intelligence(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_issues_event_intelligence
  ON issues(event_intelligence_id);

-- Disabled templates make setup obvious without pretending credentials are configured.
INSERT INTO integrations (
  integration_key, name, active, category, adapter_type, endpoint_url,
  method, auth_type, auth_config, parser_kind, parser_config,
  poll_seconds, notes
)
VALUES
(
  'NYC_PERMITTED_EVENTS',
  'NYC Permitted Events - Manhattan',
  true,
  'EVENTS',
  'HTTP',
  'https://data.cityofnewyork.us/resource/tvpp-9vvx.json',
  'GET',
  'NONE',
  '{}'::jsonb,
  'JSON_EVENTS',
  '{"list_path":"","mapping":{"id":"event_id","title":"event_name","start":"start_date_time","end":"end_date_time","event_type":"event_type","venue":"event_location","municipality":"event_borough","road_impact":"street_closure_type"},"defaults":{"municipality":"Manhattan","state":"NY","default_timezone":"America/New_York"}}'::jsonb,
  3600,
  'Authoritative NYC Open Data permitted-events feed. Query is limited to upcoming Manhattan events.'
),
(
  'NJT_RAIL_GTFS_TEMPLATE',
  'NJ TRANSIT Rail GTFS (template)',
  false,
  'TRANSIT',
  'HTTP',
  'https://raildata.njt.gov/api/GTFSRT/getGTFS',
  'POST',
  'NONE',
  '{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","note":"NJT token exchange requires adapter configuration before activation"}'::jsonb,
  'NONE',
  '{}'::jsonb,
  900,
  'Starter profile only. Use API Lab to validate your NJ TRANSIT account and endpoints before activation.'
),
(
  'TICKETMASTER_METRO_EVENTS',
  'Ticketmaster Metro Events (template)',
  false,
  'EVENTS',
  'HTTP',
  'https://app.ticketmaster.com/discovery/v2/events.json',
  'GET',
  'API_KEY_QUERY_ENV',
  '{"key_env":"TICKETMASTER_API_KEY","key_name":"apikey"}'::jsonb,
  'JSON_EVENTS',
  '{"list_path":"_embedded.events","mapping":{"id":"id","title":"name","description":"info","start":"dates.start.dateTime","status":"dates.status.code","event_type":"classifications.0.segment.name","venue":"_embedded.venues.0.name","municipality":"_embedded.venues.0.city.name","state":"_embedded.venues.0.state.stateCode","latitude":"_embedded.venues.0.location.latitude","longitude":"_embedded.venues.0.location.longitude","url":"url"},"defaults":{"default_timezone":"America/New_York"}}'::jsonb,
  3600,
  'Optional big-event discovery source. Add TICKETMASTER_API_KEY to the server environment, test, then enable.'
),
(
  'REGIONAL_EVENTS_ICS_TEMPLATE',
  'Regional Events ICS (template)',
  false,
  'EVENTS',
  'HTTP',
  'https://example.invalid/events.ics',
  'GET',
  'NONE',
  '{}'::jsonb,
  'ICS_EVENTS',
  '{"defaults":{"event_type":"EVENT"}}'::jsonb,
  900,
  'Replace the endpoint with an authoritative public calendar feed and test it before activation.'
)
ON CONFLICT (integration_key) DO NOTHING;

UPDATE integrations
SET request_query = '{"$limit":3000,"$where":"start_date_time >= current_timestamp AND event_borough = ''Manhattan''","$order":"start_date_time ASC"}'::jsonb,
    max_response_bytes = 5000000,
    updated_at = now()
WHERE integration_key='NYC_PERMITTED_EVENTS';

UPDATE integrations
SET request_query = '{"latlong":"40.7696,-74.0204","radius":"25","unit":"miles","size":"200","sort":"date,asc"}'::jsonb,
    max_response_bytes = 5000000,
    updated_at = now()
WHERE integration_key='TICKETMASTER_METRO_EVENTS';

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  integrations,
  integration_runs,
  event_intelligence
TO citymanager_app;
