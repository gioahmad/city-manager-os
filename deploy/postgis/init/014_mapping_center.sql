CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS map_layers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  layer_key text UNIQUE NOT NULL,
  name text NOT NULL,
  layer_type text NOT NULL,
  source_url text,
  attribution text,
  style jsonb NOT NULL DEFAULT '{}'::jsonb,
  active boolean NOT NULL DEFAULT true,
  default_visible boolean NOT NULL DEFAULT false,
  sort_order integer NOT NULL DEFAULT 100,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (layer_type IN ('CUSTOM_GEOJSON','XYZ'))
);

CREATE INDEX IF NOT EXISTS idx_map_layers_active
  ON map_layers(active, sort_order, name);

CREATE TABLE IF NOT EXISTS map_features (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  layer_id uuid NOT NULL REFERENCES map_layers(id) ON DELETE CASCADE,
  name text,
  properties jsonb NOT NULL DEFAULT '{}'::jsonb,
  geom geometry(Geometry,4326) NOT NULL,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_map_features_layer
  ON map_features(layer_id, active);
CREATE INDEX IF NOT EXISTS idx_map_features_geom
  ON map_features USING GIST (geom);

INSERT INTO map_layers(
  layer_key,name,layer_type,source_url,attribution,
  style,active,default_visible,sort_order
)
VALUES (
  'BASE_OSM',
  'OpenStreetMap',
  'XYZ',
  'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  '© OpenStreetMap contributors',
  '{"maxZoom":19}'::jsonb,
  true,
  true,
  10
)
ON CONFLICT (layer_key) DO NOTHING;

-- Dashboard and employee web services connect as citymanager_app.
-- Keep web GIS administration inside that existing application role.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE map_layers, map_features TO citymanager_app;
