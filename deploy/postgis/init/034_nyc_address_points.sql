BEGIN;

CREATE TABLE IF NOT EXISTS gis_nyc_addresses (
  objectid bigint PRIMARY KEY,
  addresspointid text NOT NULL,
  fulladdr text NOT NULL,
  post_comm text NOT NULL,
  post_code text,
  pcl_guid text,
  geom geometry(Point,4326) NOT NULL,
  status text NOT NULL DEFAULT 'A',
  primarypt text NOT NULL DEFAULT 'Y',
  inc_muni text NOT NULL,
  state text NOT NULL DEFAULT 'NY',
  county text NOT NULL,
  source_updated_at timestamptz
);

CREATE INDEX IF NOT EXISTS gis_nyc_addresses_geom_gix
  ON gis_nyc_addresses USING gist(geom);
CREATE INDEX IF NOT EXISTS gis_nyc_addresses_geog_gix
  ON gis_nyc_addresses USING gist((geom::geography));
CREATE INDEX IF NOT EXISTS gis_nyc_addresses_fulladdr_idx
  ON gis_nyc_addresses(lower(fulladdr));
CREATE INDEX IF NOT EXISTS gis_nyc_addresses_borough_idx
  ON gis_nyc_addresses(lower(post_comm));

GRANT SELECT ON gis_nyc_addresses TO citymanager_app;

COMMIT;
