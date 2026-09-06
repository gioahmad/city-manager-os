
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS transit_providers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_key text UNIQUE NOT NULL,
  name text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  phase integer NOT NULL DEFAULT 1 CHECK (phase BETWEEN 1 AND 9),
  modes text[] NOT NULL DEFAULT '{}',
  notes text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transit_watch_config (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id uuid REFERENCES transit_providers(id) ON DELETE CASCADE,
  active boolean NOT NULL DEFAULT true,
  target_type text NOT NULL,
  target_key text NOT NULL,
  display_name text NOT NULL,
  municipality text,
  corridor text,
  min_impact_level text NOT NULL DEFAULT 'WATCH',
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (target_type IN ('PROVIDER','MODE','ROUTE','LINE','STOP','STATION','TERMINAL','CORRIDOR','AREA')),
  CHECK (min_impact_level IN ('AWARENESS','WATCH','ALERT')),
  UNIQUE (provider_id,target_type,target_key)
);

CREATE TABLE IF NOT EXISTS transit_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id uuid NOT NULL REFERENCES transit_providers(id) ON DELETE CASCADE,
  source_integration_id uuid REFERENCES integrations(id) ON DELETE SET NULL,
  asset_key text NOT NULL,
  asset_type text NOT NULL,
  mode text,
  name text NOT NULL,
  short_name text,
  parent_asset_key text,
  municipality text,
  county text,
  state text,
  latitude double precision,
  longitude double precision,
  geom geometry(Point,4326),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  active boolean NOT NULL DEFAULT true,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider_id,asset_key)
);

CREATE INDEX IF NOT EXISTS idx_transit_assets_provider_type
  ON transit_assets(provider_id,asset_type,active);
CREATE INDEX IF NOT EXISTS idx_transit_assets_geom
  ON transit_assets USING GIST (geom);

CREATE TABLE IF NOT EXISTS transit_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_integration_id uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
  provider_id uuid NOT NULL REFERENCES transit_providers(id) ON DELETE CASCADE,
  external_key text NOT NULL,
  fingerprint text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  mode text,
  route_key text,
  route_name text,
  asset_key text,
  asset_name text,
  title text NOT NULL,
  description text,
  status text NOT NULL DEFAULT 'ACTIVE',
  municipality text,
  county text,
  state text,
  source_url text,
  impact_score integer NOT NULL DEFAULT 0 CHECK (impact_score BETWEEN 0 AND 100),
  impact_level text NOT NULL DEFAULT 'AWARENESS',
  starts_at timestamptz,
  ends_at timestamptz,
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
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (impact_level IN ('AWARENESS','WATCH','ALERT')),
  UNIQUE (source_integration_id,external_key)
);

CREATE INDEX IF NOT EXISTS idx_transit_obs_active_level
  ON transit_observations(active,impact_level,impact_score DESC);
CREATE INDEX IF NOT EXISTS idx_transit_obs_recent
  ON transit_observations(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_transit_obs_alert_pending
  ON transit_observations(alert_pending,updated_at)
  WHERE alert_pending=true;
CREATE INDEX IF NOT EXISTS idx_transit_obs_geom
  ON transit_observations USING GIST (geom);

ALTER TABLE issues
  ADD COLUMN IF NOT EXISTS transit_observation_id uuid
  REFERENCES transit_observations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_issues_transit_observation
  ON issues(transit_observation_id);

INSERT INTO transit_providers(provider_key,name,active,phase,modes,notes)
VALUES
  ('NJ_TRANSIT','NJ TRANSIT',true,1,ARRAY['BUS','RAIL','LIGHT_RAIL'],'Phase 1 provider.'),
  ('NY_WATERWAY','NY Waterway',false,2,ARRAY['FERRY'],'Phase 2 provider. Port Imperial is the highest-priority ferry context.'),
  ('PATH','PATH',false,2,ARRAY['RAIL'],'Phase 2 cross-Hudson provider.'),
  ('PORT_AUTHORITY','Port Authority',false,3,ARRAY['BUS_TERMINAL','TUNNEL','TRAFFIC'],'Phase 3 PABT / Lincoln Tunnel context.'),
  ('MTA','MTA',false,3,ARRAY['SUBWAY','BUS'],'Phase 3/4 NYC connection context.'),
  ('SEASTREAK','Seastreak',false,4,ARRAY['FERRY'],'Future regional ferry provider.')
ON CONFLICT(provider_key) DO UPDATE
SET name=EXCLUDED.name,
    phase=EXCLUDED.phase,
    modes=EXCLUDED.modes,
    notes=EXCLUDED.notes,
    updated_at=now();

UPDATE integrations
SET integration_key='NJT_RAIL_GTFS',
    name='NJ TRANSIT Rail GTFS',
    category='TRANSIT',
    adapter_type='NJT_RAIL_GTFS',
    endpoint_url='https://raildata.njt.gov/api/GTFSRT/getGTFS',
    method='POST',
    auth_type='NONE',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"GTFSRT"}'::jsonb,
    parser_kind='NONE',
    parser_config='{}'::jsonb,
    poll_seconds=86400,
    timeout_seconds=60,
    max_response_bytes=5000000,
    notes='Full NJ TRANSIT rail GTFS reference feed. Custom adapter performs token exchange, caches the token and imports route/station reference data.',
    updated_at=now()
WHERE integration_key='NJT_RAIL_GTFS_TEMPLATE'
  AND NOT EXISTS (
    SELECT 1 FROM integrations WHERE integration_key='NJT_RAIL_GTFS'
  );

INSERT INTO integrations(
  integration_key,name,active,category,adapter_type,endpoint_url,
  method,auth_type,auth_config,request_headers,request_query,request_body,
  parser_kind,parser_config,poll_seconds,timeout_seconds,max_response_bytes,
  allow_redirects,verify_tls,notes
)
VALUES
(
  'NJT_BUS_LIVE',
  'NJ TRANSIT Bus Live',
  false,
  'TRANSIT',
  'NJT_BUSDATA',
  'https://busdata.njtransit.com/NJTBusData.asmx/getBusVehicleDataXML2',
  'POST',
  'NONE',
  '{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD"}'::jsonb,
  '{}'::jsonb,'{}'::jsonb,NULL,'NONE','{}'::jsonb,
  120,30,5000000,true,true,
  'Targeted/live bus adapter. Stores relevant regional vehicle context without generating an alert for every vehicle.'
),
(
  'NJT_RAIL_LIVE',
  'NJ TRANSIT Rail Live',
  false,
  'TRANSIT',
  'NJT_RAILDATA',
  'https://raildata.njt.gov/api/TrainData/getStationMSG',
  'POST',
  'NONE',
  '{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"TrainData"}'::jsonb,
  '{}'::jsonb,'{}'::jsonb,NULL,'NONE','{}'::jsonb,
  180,30,5000000,true,true,
  'Targeted RailData adapter. Loads station reference data and messages for watched stations using a cached NJT token.'
),
(
  'NJT_BUS_GTFS',
  'NJ TRANSIT Bus GTFS',
  false,
  'TRANSIT',
  'NJT_BUS_GTFS',
  'https://developer.njtransit.com/registration/appdoc?app=GTFS-BUS',
  'GET',
  'NONE',
  '{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","download_url_env":"NJT_BUS_GTFS_URL"}'::jsonb,
  '{}'::jsonb,'{}'::jsonb,NULL,'NONE','{}'::jsonb,
  86400,60,5000000,true,true,
  'Full bus GTFS adapter foundation. NJT_BUS_GTFS_URL must be set from the authenticated NJ TRANSIT developer documentation before activation.'
),
(
  'NJT_RAIL_GTFS',
  'NJ TRANSIT Rail GTFS',
  false,
  'TRANSIT',
  'NJT_RAIL_GTFS',
  'https://raildata.njt.gov/api/GTFSRT/getGTFS',
  'POST',
  'NONE',
  '{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"GTFSRT"}'::jsonb,
  '{}'::jsonb,'{}'::jsonb,NULL,'NONE','{}'::jsonb,
  86400,60,5000000,true,true,
  'Full rail GTFS adapter. Token is cached and reused; routes/stations are imported locally.'
)
ON CONFLICT(integration_key) DO UPDATE
SET name=EXCLUDED.name,
    category=EXCLUDED.category,
    adapter_type=EXCLUDED.adapter_type,
    endpoint_url=EXCLUDED.endpoint_url,
    method=EXCLUDED.method,
    auth_type=EXCLUDED.auth_type,
    auth_config=EXCLUDED.auth_config,
    parser_kind=EXCLUDED.parser_kind,
    parser_config=EXCLUDED.parser_config,
    poll_seconds=EXCLUDED.poll_seconds,
    timeout_seconds=EXCLUDED.timeout_seconds,
    max_response_bytes=EXCLUDED.max_response_bytes,
    allow_redirects=EXCLUDED.allow_redirects,
    verify_tls=EXCLUDED.verify_tls,
    notes=EXCLUDED.notes,
    updated_at=now();

INSERT INTO transit_watch_config(
  provider_id,active,target_type,target_key,display_name,municipality,corridor,min_impact_level,notes
)
SELECT p.id,true,v.target_type,v.target_key,v.display_name,v.municipality,v.corridor,v.min_level,v.notes
FROM transit_providers p
CROSS JOIN (
  VALUES
    ('CORRIDOR','LINCOLN TUNNEL','Lincoln Tunnel / Route 495 / PABT Corridor','Weehawken','Lincoln Tunnel / Route 495 / PABT','WATCH','Primary cross-Hudson bus and roadway corridor.'),
    ('TERMINAL','PORT IMPERIAL','Port Imperial','Weehawken','Port Imperial / Waterfront','WATCH','High-priority Weehawken transportation hub.'),
    ('STATION','HOBOKEN','Hoboken Terminal','Hoboken','Hoboken Terminal','WATCH','Major NJT/PATH/ferry connection.'),
    ('STATION','SECAUCUS','Secaucus Junction','Secaucus','Secaucus Junction','WATCH','Major NJT transfer point.'),
    ('STATION','NEW YORK PENN','New York Penn Station','New York','Penn Station','WATCH','Major Manhattan NJT terminal.'),
    ('STATION','LINCOLN HARBOR','Lincoln Harbor','Weehawken','Waterfront / HBLR','WATCH','Local HBLR / ferry context.'),
    ('STATION','BERGENLINE','Bergenline Avenue HBLR','Union City','HBLR','WATCH','Nearby HBLR context.')
) AS v(target_type,target_key,display_name,municipality,corridor,min_level,notes)
WHERE p.provider_key='NJ_TRANSIT'
ON CONFLICT(provider_id,target_type,target_key) DO UPDATE
SET display_name=EXCLUDED.display_name,
    municipality=EXCLUDED.municipality,
    corridor=EXCLUDED.corridor,
    min_impact_level=EXCLUDED.min_impact_level,
    notes=EXCLUDED.notes,
    updated_at=now();

GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE
  transit_providers,
  transit_watch_config,
  transit_assets,
  transit_observations
TO citymanager_app;
