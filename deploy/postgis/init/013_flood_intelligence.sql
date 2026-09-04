CREATE TABLE IF NOT EXISTS gis_flood_zones (
  id bigserial PRIMARY KEY,
  dfirm_id text,
  fld_zone text,
  zone_subty text,
  sfha_tf text,
  static_bfe double precision,
  depth double precision,
  velocity double precision,
  source_objectid bigint,
  geom geometry(MultiPolygon,4326) NOT NULL,
  source_updated_at timestamptz,
  imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gis_flood_zones_geom ON gis_flood_zones USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_gis_flood_zones_zone ON gis_flood_zones (fld_zone, zone_subty);

CREATE TABLE IF NOT EXISTS flood_observations (
  id bigserial PRIMARY KEY,
  source text NOT NULL,
  station_id text,
  observed_at timestamptz NOT NULL,
  water_level_mhhw_ft double precision,
  predicted_level_mhhw_ft double precision,
  flood_category text,
  title text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source, station_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_flood_observations_observed ON flood_observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_flood_observations_category ON flood_observations(flood_category);

CREATE OR REPLACE FUNCTION public.gis_flood_risk_for_point(p_lat double precision, p_lon double precision)
RETURNS TABLE(
  fld_zone text,
  zone_subty text,
  sfha_tf text,
  static_bfe double precision
)
LANGUAGE sql STABLE AS $$
  SELECT z.fld_zone,z.zone_subty,z.sfha_tf,z.static_bfe
  FROM gis_flood_zones z
  WHERE ST_Covers(z.geom, ST_SetSRID(ST_MakePoint(p_lon,p_lat),4326))
  ORDER BY CASE WHEN z.sfha_tf='T' THEN 0 ELSE 1 END, z.fld_zone
  LIMIT 10;
$$;

CREATE OR REPLACE FUNCTION public.gis_watch_items_in_flood_zones()
RETURNS TABLE(
  watch_id text,
  display_name text,
  address text,
  fld_zone text,
  zone_subty text,
  sfha_tf text
)
LANGUAGE sql STABLE AS $$
  SELECT w.watch_id,w.display_name,w.address,z.fld_zone,z.zone_subty,z.sfha_tf
  FROM watch_items w
  JOIN gis_flood_zones z ON w.geom IS NOT NULL AND ST_Intersects(z.geom,w.geom)
  WHERE w.active=true
  ORDER BY w.display_name,z.fld_zone;
$$;
