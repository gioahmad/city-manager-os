#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
EXPECTED_TARGET="${1:-}"
BACKUP_DIR="/var/backups/city-manager-os"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "#58 DATABASE INSTALL FAILED rc=${rc} line=${LINENO}"; exit "$rc"' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] || fail "HEAD does not match expected target"
[[ "$(docker inspect --format '{{.State.Running}}' citymanager-postgis)" == true ]] || fail "PostGIS is not running"

install -d -m 700 "$BACKUP_DIR"
SCHEMA_BACKUP="$BACKUP_DIR/pre-58-schema-${STAMP}.sql.gz"
WATCH_BACKUP="$BACKUP_DIR/pre-58-watch-routing-${STAMP}.dump"

log "Backing up schema and the existing Watchlist routing tables"
docker exec citymanager-postgis sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner --no-privileges' \
  | gzip -9 > "$SCHEMA_BACKUP"
docker exec citymanager-postgis sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --data-only --table=watch_items --table=watch_item_recipients' \
  > "$WATCH_BACKUP"
chmod 600 "$SCHEMA_BACKUP" "$WATCH_BACKUP"
[[ -s "$SCHEMA_BACKUP" && -s "$WATCH_BACKUP" ]] || fail "backup creation failed"

log "Applying additive #58 catalog and topology migration"
docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < deploy/postgis/init/031_spatial_reference_catalog.sql

log "Validating parcel-context SQL before the source refresh"
FUNCTION_STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SET ROLE citymanager_app;
SELECT gis_parcel_context(NULL::integer,500.0) IS NULL;
RESET ROLE;
SQL
)"
[[ "$FUNCTION_STATE" == "t" ]] || fail "parcel-context SQL validation failed: ${FUNCTION_STATE}"

log "Linking existing Hudson parcel facilities, NG911 landmarks, and transit assets"
docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT spatial_reference_refresh_local_sources() AS refresh_result;
SQL

log "Verifying schema, linked sources, functions, grants, and a real NJ parcel context"
STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq' <<'SQL'
SELECT to_regclass('public.spatial_reference_entities') IS NOT NULL;
SELECT to_regclass('public.spatial_reference_catalog_status') IS NOT NULL;
SELECT EXISTS (
  SELECT 1 FROM information_schema.columns
  WHERE table_schema='public' AND table_name='watch_items'
    AND column_name='spatial_reference_entity_id'
);
SELECT count(*)=2 FROM information_schema.columns
WHERE table_schema='public' AND table_name='watch_items'
  AND column_name IN ('spatial_geom','spatial_scope');
SELECT count(DISTINCT p.proname)=7 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.prokind='f'
  AND p.proname IN ('spatial_reference_refresh_local_sources','gis_parcel_for_point',
    'gis_addresses_for_parcel','gis_adjoining_parcels','gis_parcels_within_radius',
    'gis_spatial_impact_context','gis_parcel_context');
SELECT count(*)>0 FROM spatial_reference_entities WHERE active=true;
SELECT count(*)>0 FROM spatial_reference_entities
WHERE active=true AND upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
  AND parcel_objectid IS NOT NULL;
SELECT count(*)>0 AND count(*)=(
  SELECT count(*) FROM transit_assets WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom)
) FROM spatial_reference_entities WHERE active=true AND source_provider='CMOS_TRANSIT_ASSET';
SET ROLE citymanager_app;
SELECT count(*)>=0 FROM spatial_reference_entities;
WITH sample AS (
  SELECT objectid FROM gis_parcels
  WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%'
  ORDER BY objectid LIMIT 1
)
SELECT EXISTS (SELECT 1 FROM sample WHERE gis_parcel_context(objectid,500.0) IS NOT NULL);
WITH sample AS (
  SELECT ST_Y(ST_PointOnSurface(geom)) AS lat,ST_X(ST_PointOnSurface(geom)) AS lon
  FROM gis_parcels WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%'
  ORDER BY objectid LIMIT 1
)
SELECT EXISTS (SELECT 1 FROM sample s, LATERAL gis_parcel_for_point(s.lat,s.lon,3.0));
RESET ROLE;
SQL
)"
EXPECTED=$'t\nt\nt\nt\nt\nt\nt\nt\nt\nt\nt'
[[ "$STATE" == "$EXPECTED" ]] || fail "unexpected verification state: ${STATE}"

log "#58 DATABASE INSTALL: PASS"
log "Schema backup: ${SCHEMA_BACKUP}"
log "Watch/routing backup: ${WATCH_BACKUP}"
log "Existing source geometry was linked into the new catalog. Statewide GIS source tables were not copied or changed."
log "No watches, subscribers, routes, deliveries, notifications, or n8n workflows were created or changed."
