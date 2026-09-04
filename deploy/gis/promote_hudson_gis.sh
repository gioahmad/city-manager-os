#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BUILD_ID="$(date '+%Y%m%d%H%M%S')"
PARCEL_BUILD="gis_parcels_build_${BUILD_ID}"
ADDRESS_BUILD="gis_addresses_build_${BUILD_ID}"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "HUDSON GIS PRODUCTION PROMOTION FAILED with exit code ${rc}. Existing production tables were not replaced unless the atomic swap had already committed."; exit $rc' ERR

for cmd in git docker; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

cd "$REPO"
log "Synchronizing repository with origin/main"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean. No production GIS changes were made."
fi
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ ${HEAD_SHA}"

log "Checking required staging tables and columns"
MISSING="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
WITH required(table_name,column_name) AS (
  VALUES
    ('stg_hudson_parcels','geom'),
    ('stg_hudson_parcels','objectid'),
    ('stg_hudson_parcels','pams_pin'),
    ('stg_hudson_parcels','pcl_mun'),
    ('stg_hudson_parcels','pclblock'),
    ('stg_hudson_parcels','pcllot'),
    ('stg_hudson_parcels','pclqcode'),
    ('stg_hudson_parcels','gis_pin'),
    ('stg_hudson_parcels','pcl_guid'),
    ('stg_hudson_parcels','mun_name'),
    ('stg_hudson_parcels','prop_loc'),
    ('stg_hudson_addresses','geom'),
    ('stg_hudson_addresses','objectid'),
    ('stg_hudson_addresses','status'),
    ('stg_hudson_addresses','fulladdr'),
    ('stg_hudson_addresses','pcl_guid'),
    ('stg_hudson_addresses','post_comm'),
    ('stg_hudson_addresses','post_code')
), missing AS (
  SELECT r.table_name,r.column_name
  FROM required r
  LEFT JOIN information_schema.columns c
    ON c.table_schema='public'
   AND c.table_name=r.table_name
   AND c.column_name=r.column_name
  WHERE c.column_name IS NULL
)
SELECT table_name || '.' || column_name FROM missing ORDER BY 1;
SQL
)"
if [[ -n "$MISSING" ]]; then
  printf '%s\n' "$MISSING"
  fail "Required staging columns are missing."
fi

log "Validating staging counts, geometry and SRID"
STAGE_CHECK="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
WITH stats AS (
  SELECT 'parcels' AS dataset,count(*) AS rows,
         count(*) FILTER (WHERE geom IS NULL OR NOT ST_IsValid(geom)) AS bad_geom,
         min(ST_SRID(geom)) AS min_srid,max(ST_SRID(geom)) AS max_srid
  FROM stg_hudson_parcels
  UNION ALL
  SELECT 'addresses',count(*),
         count(*) FILTER (WHERE geom IS NULL OR NOT ST_IsValid(geom)),
         min(ST_SRID(geom)),max(ST_SRID(geom))
  FROM stg_hudson_addresses
)
SELECT dataset || '|' || rows || '|' || bad_geom || '|' || min_srid || '|' || max_srid
FROM stats ORDER BY dataset;
SQL
)"
printf '%s\n' "$STAGE_CHECK"
while IFS='|' read -r dataset rows bad_geom min_srid max_srid; do
  [[ -n "$dataset" ]] || continue
  (( rows > 0 )) || fail "$dataset staging table is empty"
  (( bad_geom == 0 )) || fail "$dataset has $bad_geom null/invalid geometries"
  [[ "$min_srid" == "4326" && "$max_srid" == "4326" ]] || fail "$dataset has unexpected SRID range ${min_srid}-${max_srid}"
done <<< "$STAGE_CHECK"

log "Building indexed candidate production tables"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
DROP TABLE IF EXISTS public.${PARCEL_BUILD};
DROP TABLE IF EXISTS public.${ADDRESS_BUILD};

CREATE TABLE public.${PARCEL_BUILD} AS TABLE public.stg_hudson_parcels;
CREATE INDEX ${PARCEL_BUILD}_geom_gix ON public.${PARCEL_BUILD} USING GIST (geom);
CREATE INDEX ${PARCEL_BUILD}_objectid_idx ON public.${PARCEL_BUILD} (objectid);
CREATE INDEX ${PARCEL_BUILD}_pcl_guid_idx ON public.${PARCEL_BUILD} (pcl_guid) WHERE pcl_guid IS NOT NULL;
CREATE INDEX ${PARCEL_BUILD}_pams_pin_idx ON public.${PARCEL_BUILD} (pams_pin) WHERE pams_pin IS NOT NULL;
CREATE INDEX ${PARCEL_BUILD}_gis_pin_idx ON public.${PARCEL_BUILD} (gis_pin) WHERE gis_pin IS NOT NULL;
CREATE INDEX ${PARCEL_BUILD}_block_lot_idx ON public.${PARCEL_BUILD} (pcl_mun,pclblock,pcllot);
CREATE INDEX ${PARCEL_BUILD}_mun_name_idx ON public.${PARCEL_BUILD} (lower(mun_name));
CREATE INDEX ${PARCEL_BUILD}_prop_loc_idx ON public.${PARCEL_BUILD} (lower(prop_loc));
ANALYZE public.${PARCEL_BUILD};

CREATE TABLE public.${ADDRESS_BUILD} AS TABLE public.stg_hudson_addresses;
CREATE INDEX ${ADDRESS_BUILD}_geom_gix ON public.${ADDRESS_BUILD} USING GIST (geom);
CREATE INDEX ${ADDRESS_BUILD}_geog_gix ON public.${ADDRESS_BUILD} USING GIST ((geom::geography));
CREATE INDEX ${ADDRESS_BUILD}_objectid_idx ON public.${ADDRESS_BUILD} (objectid);
CREATE INDEX ${ADDRESS_BUILD}_status_idx ON public.${ADDRESS_BUILD} (status);
CREATE INDEX ${ADDRESS_BUILD}_pcl_guid_idx ON public.${ADDRESS_BUILD} (pcl_guid) WHERE pcl_guid IS NOT NULL;
CREATE INDEX ${ADDRESS_BUILD}_fulladdr_idx ON public.${ADDRESS_BUILD} (lower(fulladdr));
CREATE INDEX ${ADDRESS_BUILD}_post_comm_idx ON public.${ADDRESS_BUILD} (lower(post_comm));
CREATE INDEX ${ADDRESS_BUILD}_post_code_idx ON public.${ADDRESS_BUILD} (post_code);
ANALYZE public.${ADDRESS_BUILD};
SQL

log "Validating candidate row counts against staging"
COUNT_CHECK="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<SQL
SELECT
  (SELECT count(*) FROM stg_hudson_parcels)::text || '|' ||
  (SELECT count(*) FROM ${PARCEL_BUILD})::text || '|' ||
  (SELECT count(*) FROM stg_hudson_addresses)::text || '|' ||
  (SELECT count(*) FROM ${ADDRESS_BUILD})::text;
SQL
)"
IFS='|' read -r STG_P PARCEL_P STG_A ADDRESS_P <<< "$COUNT_CHECK"
[[ "$STG_P" == "$PARCEL_P" ]] || fail "Parcel candidate row count mismatch: staging=$STG_P candidate=$PARCEL_P"
[[ "$STG_A" == "$ADDRESS_P" ]] || fail "Address candidate row count mismatch: staging=$STG_A candidate=$ADDRESS_P"
log "Candidate counts validated: parcels=${PARCEL_P}, addresses=${ADDRESS_P}"

log "Atomically promoting candidate tables; preserving any existing production tables"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
BEGIN;
DO \$\$
BEGIN
  IF to_regclass('public.gis_parcels') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE public.gis_parcels RENAME TO gis_parcels_backup_${BUILD_ID}';
  END IF;
  IF to_regclass('public.gis_addresses') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE public.gis_addresses RENAME TO gis_addresses_backup_${BUILD_ID}';
  END IF;
END
\$\$;
ALTER TABLE public.${PARCEL_BUILD} RENAME TO gis_parcels;
ALTER TABLE public.${ADDRESS_BUILD} RENAME TO gis_addresses;
COMMIT;
SQL

log "Installing production GIS lookup functions"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
CREATE OR REPLACE FUNCTION public.gis_parcel_by_block_lot(
  p_municipality text,
  p_block text,
  p_lot text
)
RETURNS TABLE(
  pams_pin text,
  gis_pin text,
  parcel_guid text,
  municipality text,
  block text,
  lot text,
  qualifier text,
  property_location text
)
LANGUAGE sql STABLE AS $$
  SELECT p.pams_pin,p.gis_pin,p.pcl_guid,p.mun_name,p.pclblock,p.pcllot,p.pclqcode,p.prop_loc
  FROM public.gis_parcels p
  WHERE lower(trim(coalesce(p.mun_name,''))) = lower(trim(coalesce(p_municipality,'')))
    AND trim(coalesce(p.pclblock,'')) = trim(coalesce(p_block,''))
    AND trim(coalesce(p.pcllot,'')) = trim(coalesce(p_lot,''))
  ORDER BY p.pclqcode NULLS FIRST,p.pams_pin
  LIMIT 50;
$$;

CREATE OR REPLACE FUNCTION public.gis_parcel_for_address(
  p_full_address text,
  p_postal_community text DEFAULT NULL
)
RETURNS TABLE(
  address_objectid bigint,
  full_address text,
  address_status text,
  parcel_guid text,
  pams_pin text,
  gis_pin text,
  municipality text,
  block text,
  lot text,
  property_location text,
  match_method text
)
LANGUAGE sql STABLE AS $$
  WITH matched_address AS (
    SELECT a.*
    FROM public.gis_addresses a
    WHERE lower(trim(coalesce(a.fulladdr,''))) = lower(trim(coalesce(p_full_address,'')))
      AND (
        p_postal_community IS NULL
        OR lower(trim(coalesce(a.post_comm,''))) = lower(trim(p_postal_community))
      )
    ORDER BY CASE WHEN a.status='A' THEN 0 ELSE 1 END,a.objectid
    LIMIT 1
  )
  SELECT
    a.objectid::bigint,
    a.fulladdr,
    a.status,
    coalesce(d.pcl_guid,s.pcl_guid),
    coalesce(d.pams_pin,s.pams_pin),
    coalesce(d.gis_pin,s.gis_pin),
    coalesce(d.mun_name,s.mun_name),
    coalesce(d.pclblock,s.pclblock),
    coalesce(d.pcllot,s.pcllot),
    coalesce(d.prop_loc,s.prop_loc),
    CASE WHEN d.objectid IS NOT NULL THEN 'PCL_GUID'
         WHEN s.objectid IS NOT NULL THEN 'SPATIAL'
         ELSE 'NO_PARCEL_MATCH' END
  FROM matched_address a
  LEFT JOIN LATERAL (
    SELECT p.objectid,p.pcl_guid,p.pams_pin,p.gis_pin,p.mun_name,p.pclblock,p.pcllot,p.prop_loc
    FROM public.gis_parcels p
    WHERE a.pcl_guid IS NOT NULL AND p.pcl_guid=a.pcl_guid
    LIMIT 1
  ) d ON true
  LEFT JOIN LATERAL (
    SELECT p.objectid,p.pcl_guid,p.pams_pin,p.gis_pin,p.mun_name,p.pclblock,p.pcllot,p.prop_loc
    FROM public.gis_parcels p
    WHERE d.objectid IS NULL AND ST_Covers(p.geom,a.geom)
    LIMIT 1
  ) s ON true;
$$;

CREATE OR REPLACE FUNCTION public.gis_nearby_addresses(
  p_lat double precision,
  p_lon double precision,
  p_radius_ft double precision,
  p_limit integer DEFAULT 50
)
RETURNS TABLE(
  address_objectid bigint,
  full_address text,
  postal_community text,
  postal_code text,
  status text,
  distance_ft double precision,
  longitude double precision,
  latitude double precision
)
LANGUAGE sql STABLE AS $$
  WITH origin AS (
    SELECT ST_SetSRID(ST_MakePoint(p_lon,p_lat),4326) AS geom
  )
  SELECT
    a.objectid::bigint,
    a.fulladdr,
    a.post_comm,
    a.post_code,
    a.status,
    ST_Distance(a.geom::geography,o.geom::geography) / 0.3048 AS distance_ft,
    ST_X(a.geom)::double precision,
    ST_Y(a.geom)::double precision
  FROM public.gis_addresses a
  CROSS JOIN origin o
  WHERE p_radius_ft > 0
    AND ST_DWithin(a.geom::geography,o.geom::geography,p_radius_ft * 0.3048)
  ORDER BY ST_Distance(a.geom::geography,o.geom::geography)
  LIMIT greatest(1,least(coalesce(p_limit,50),500));
$$;
SQL

log "Recording GIS dataset versions"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
INSERT INTO gis_dataset_versions(dataset_id,dataset_name,source_url,imported_at,row_count,status,notes)
SELECT
  'NJOGIS_HUDSON_PARCELS',
  'NJOGIS Hudson County Parcels / MOD-IV',
  'https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/ArcGIS/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0',
  now(),count(*),'ACTIVE','Production promotion ${BUILD_ID}; repository ${HEAD_SHA}'
FROM gis_parcels
ON CONFLICT (dataset_id) DO UPDATE
SET dataset_name=EXCLUDED.dataset_name,
    source_url=EXCLUDED.source_url,
    imported_at=EXCLUDED.imported_at,
    row_count=EXCLUDED.row_count,
    status=EXCLUDED.status,
    notes=EXCLUDED.notes;

INSERT INTO gis_dataset_versions(dataset_id,dataset_name,source_url,imported_at,row_count,status,notes)
SELECT
  'NJOGIS_HUDSON_ADDRESSES',
  'NJOGIS Hudson County NG911 Address Points',
  'https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/ArcGIS/rest/services/AddressPoints/FeatureServer/0',
  now(),count(*),'ACTIVE','Production promotion ${BUILD_ID}; repository ${HEAD_SHA}'
FROM gis_addresses
ON CONFLICT (dataset_id) DO UPDATE
SET dataset_name=EXCLUDED.dataset_name,
    source_url=EXCLUDED.source_url,
    imported_at=EXCLUDED.imported_at,
    row_count=EXCLUDED.row_count,
    status=EXCLUDED.status,
    notes=EXCLUDED.notes;
SQL

log "Running real-data production lookup tests"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'PRODUCTION_COUNTS' AS test,
       (SELECT count(*) FROM gis_parcels) AS parcels,
       (SELECT count(*) FROM gis_addresses) AS addresses;

WITH sample AS (
  SELECT mun_name,pclblock,pcllot
  FROM gis_parcels
  WHERE nullif(trim(mun_name),'') IS NOT NULL
    AND nullif(trim(pclblock),'') IS NOT NULL
    AND nullif(trim(pcllot),'') IS NOT NULL
  ORDER BY objectid
  LIMIT 1
)
SELECT 'BLOCK_LOT_TEST' AS test,s.*,r.pams_pin,r.gis_pin,r.property_location
FROM sample s
CROSS JOIN LATERAL gis_parcel_by_block_lot(s.mun_name,s.pclblock,s.pcllot) r
LIMIT 5;

WITH sample AS (
  SELECT fulladdr,post_comm
  FROM gis_addresses
  WHERE nullif(trim(fulladdr),'') IS NOT NULL
  ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,objectid
  LIMIT 1
)
SELECT 'ADDRESS_PARCEL_TEST' AS test,s.*,r.*
FROM sample s
CROSS JOIN LATERAL gis_parcel_for_address(s.fulladdr,s.post_comm) r;

WITH sample AS (
  SELECT ST_Y(geom) AS lat,ST_X(geom) AS lon
  FROM gis_addresses
  WHERE geom IS NOT NULL
  ORDER BY objectid
  LIMIT 1
)
SELECT 'NEARBY_TEST' AS test,n.*
FROM sample s
CROSS JOIN LATERAL gis_nearby_addresses(s.lat,s.lon,500,5) n;
SQL

log "HUDSON GIS PRODUCTION PASSED"
log "Production tables: gis_parcels, gis_addresses"
log "Lookups: gis_parcel_by_block_lot, gis_parcel_for_address, gis_nearby_addresses"
log "Repository commit: ${HEAD_SHA}"
