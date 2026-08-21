CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS watch_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  watch_id text UNIQUE NOT NULL,
  active boolean NOT NULL DEFAULT true,
  watch_type text NOT NULL,
  display_name text NOT NULL,
  search_term text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  match_mode text NOT NULL,
  match_field text,
  category text,
  subcategory text,
  parent_group text,
  tags text[] NOT NULL DEFAULT '{}',
  source_filter text[] NOT NULL DEFAULT '{}',
  alert_category_filter text[] NOT NULL DEFAULT '{}',
  min_priority integer NOT NULL DEFAULT 1 CHECK (min_priority BETWEEN 1 AND 5),
  starts_at timestamptz,
  expires_at timestamptz,
  address text,
  municipality text,
  county text,
  state text,
  zip text,
  block text,
  lot text,
  qualifier text,
  parcel_id text,
  latitude double precision,
  longitude double precision,
  gis_enabled boolean NOT NULL DEFAULT false,
  gis_lookup text,
  nearby_enabled boolean NOT NULL DEFAULT false,
  radius_ft double precision,
  source_notes text,
  notes text,
  geom geometry(Point, 4326),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (match_mode IN ('FIELD','CONTAINS','WORD','EXACT')),
  CHECK (match_mode <> 'FIELD' OR match_field IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_watch_items_active ON watch_items(active);
CREATE INDEX IF NOT EXISTS idx_watch_items_type ON watch_items(watch_type);
CREATE INDEX IF NOT EXISTS idx_watch_items_geom ON watch_items USING GIST (geom);

CREATE TABLE IF NOT EXISTS subscribers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscriber_id text UNIQUE NOT NULL,
  name text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  ntfy_topic text NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watch_item_recipients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  watch_item_id uuid NOT NULL REFERENCES watch_items(id) ON DELETE CASCADE,
  subscriber_id uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  active boolean NOT NULL DEFAULT true,
  UNIQUE (watch_item_id, subscriber_id)
);

CREATE TABLE IF NOT EXISTS alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  schema_version text NOT NULL DEFAULT '1.0',
  alert_id text UNIQUE NOT NULL,
  source text NOT NULL,
  source_event_id text,
  category text NOT NULL,
  subtype text NOT NULL,
  status text NOT NULL,
  event_action text NOT NULL,
  title text NOT NULL,
  message text NOT NULL,
  priority integer NOT NULL CHECK (priority BETWEEN 1 AND 5),
  county text,
  municipality text,
  location jsonb NOT NULL DEFAULT '{}'::jsonb,
  tags text[] NOT NULL DEFAULT '{}',
  click_url text,
  source_url text,
  observed_at timestamptz,
  source_updated_at timestamptz,
  received_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  raw_payload jsonb,
  search_text text,
  geom geometry(Point, 4326),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts(source);
CREATE INDEX IF NOT EXISTS idx_alerts_category ON alerts(category);
CREATE INDEX IF NOT EXISTS idx_alerts_municipality ON alerts(municipality);
CREATE INDEX IF NOT EXISTS idx_alerts_received_at ON alerts(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_geom ON alerts USING GIST (geom);

CREATE TABLE IF NOT EXISTS alert_watch_matches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  watch_item_id uuid NOT NULL REFERENCES watch_items(id) ON DELETE CASCADE,
  match_type text NOT NULL,
  match_reason text,
  matched_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (alert_id, watch_item_id, match_type)
);

CREATE TABLE IF NOT EXISTS deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delivery_key text UNIQUE NOT NULL,
  alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  subscriber_id uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  ntfy_topic text NOT NULL,
  status text NOT NULL,
  attempted_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  error_message text,
  matched_watch_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  match_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
  ntfy_message_id text,
  ntfy_response jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deliveries_alert ON deliveries(alert_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_subscriber ON deliveries(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);

CREATE TABLE IF NOT EXISTS source_health (
  source_id text PRIMARY KEY,
  status text NOT NULL,
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  last_event_at timestamptz,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS issues (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  description text,
  category text,
  priority integer CHECK (priority BETWEEN 1 AND 5),
  status text NOT NULL DEFAULT 'OPEN',
  source text,
  address text,
  municipality text,
  assigned_to text,
  watch_item_id uuid REFERENCES watch_items(id) ON DELETE SET NULL,
  geom geometry(Point, 4326),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);

CREATE TABLE IF NOT EXISTS gis_dataset_versions (
  dataset_id text PRIMARY KEY,
  dataset_name text NOT NULL,
  source_url text,
  source_updated_at timestamptz,
  imported_at timestamptz NOT NULL DEFAULT now(),
  row_count bigint,
  status text NOT NULL,
  notes text
);

-- GIS layer tables (gis_parcels, gis_addresses, gis_flood_zones, etc.)
-- are intentionally created later when their actual source schemas are known.
