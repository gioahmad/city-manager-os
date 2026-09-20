#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
EXPECTED_TARGET="${1:-}"
BACKUP_DIR="/var/backups/city-manager-os"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "#56 DATABASE INSTALL FAILED rc=${rc} line=${LINENO}"; exit "$rc"' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ "$(docker inspect --format '{{.State.Running}}' citymanager-postgis)" == true ]] \
  || fail "PostGIS is not running"

install -d -m 700 "$BACKUP_DIR"
SCHEMA_BACKUP="$BACKUP_DIR/pre-56-schema-${STAMP}.sql.gz"
WATCH_BACKUP="$BACKUP_DIR/pre-56-watch-routing-${STAMP}.dump"

log "Backing up schema and existing Watchlist routing data"
docker exec citymanager-postgis sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner --no-privileges' \
  | gzip -9 > "$SCHEMA_BACKUP"
docker exec citymanager-postgis sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --data-only --table=watch_items --table=watch_item_recipients' \
  > "$WATCH_BACKUP"
chmod 600 "$SCHEMA_BACKUP" "$WATCH_BACKUP"
[[ -s "$SCHEMA_BACKUP" && -s "$WATCH_BACKUP" ]] || fail "backup creation failed"

log "Applying additive #56 spatial-watch migration"
docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < deploy/postgis/init/032_unified_spatial_watch_pack.sql

log "Testing stored, supplied, resolver-backed, filtered, and unresolved spatial behavior"
TEST_STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
BEGIN;
INSERT INTO watch_items(
  id,watch_id,active,watch_type,display_name,search_term,match_mode,min_priority,
  nearby_enabled,radius_ft,spatial_scope,spatial_target_geom,source_filter
) VALUES
  ('56000000-0000-0000-0000-000000000001','CMOS56_IN',true,'AREA','CMOS 56 in','never-text-match','CONTAINS',1,
   true,500,'RADIUS',ST_SetSRID(ST_MakePoint(-74.0200,40.7600),4326),ARRAY['SYSTEM_TEST']),
  ('56000000-0000-0000-0000-000000000002','CMOS56_FUTURE',true,'AREA','CMOS 56 future','never-text-match','CONTAINS',1,
   true,500,'RADIUS',ST_SetSRID(ST_MakePoint(-74.0200,40.7600),4326),ARRAY['SYSTEM_TEST']),
  ('56000000-0000-0000-0000-000000000003','CMOS56_EXPIRED',true,'AREA','CMOS 56 expired','never-text-match','CONTAINS',1,
   true,500,'RADIUS',ST_SetSRID(ST_MakePoint(-74.0200,40.7600),4326),ARRAY['SYSTEM_TEST']),
  ('56000000-0000-0000-0000-000000000004','CMOS56_FILTERED',true,'AREA','CMOS 56 filtered','never-text-match','CONTAINS',1,
   true,500,'RADIUS',ST_SetSRID(ST_MakePoint(-74.0200,40.7600),4326),ARRAY['OTHER_SOURCE']),
  ('56000000-0000-0000-0000-000000000005','CMOS56_CORRIDOR',true,'CORRIDOR','CMOS 56 corridor','never-text-match','CONTAINS',1,
   true,250,'RADIUS',ST_SetSRID(ST_MakeLine(ST_MakePoint(-74.0210,40.7600),ST_MakePoint(-74.0190,40.7600)),4326),ARRAY['CORRIDOR_TEST']);
UPDATE watch_items SET starts_at=now()+interval '1 hour' WHERE watch_id='CMOS56_FUTURE';
UPDATE watch_items SET expires_at=now()-interval '1 hour' WHERE watch_id='CMOS56_EXPIRED';

INSERT INTO alerts(
  id,alert_id,source,category,subtype,status,event_action,title,message,priority,location,geom
) VALUES
  ('56000000-0000-0000-0000-000000000011','CMOS56:IN','SYSTEM_TEST','TEST','SPATIAL','ACTIVE','NEW','Spatial in','test',2,'{}',
   ST_SetSRID(ST_MakePoint(-74.0200,40.7605),4326)),
  ('56000000-0000-0000-0000-000000000012','CMOS56:OUT','SYSTEM_TEST','TEST','SPATIAL','ACTIVE','NEW','Spatial out','test',2,'{}',
   ST_SetSRID(ST_MakePoint(-74.0200,40.7700),4326)),
  ('56000000-0000-0000-0000-000000000013','CMOS56:UNRESOLVED','SYSTEM_TEST','TEST','SPATIAL','ACTIVE','NEW','Spatial unresolved','test',2,'{}',NULL),
  ('56000000-0000-0000-0000-000000000014','CMOS56:CORRIDOR_IN','CORRIDOR_TEST','TEST','SPATIAL','ACTIVE','NEW','Corridor in','test',2,'{}',
   ST_SetSRID(ST_MakePoint(-74.0200,40.7605),4326)),
  ('56000000-0000-0000-0000-000000000015','CMOS56:CORRIDOR_OUT','CORRIDOR_TEST','TEST','SPATIAL','ACTIVE','NEW','Corridor out','test',2,'{}',
   ST_SetSRID(ST_MakePoint(-74.0200,40.7700),4326)),
  ('56000000-0000-0000-0000-000000000016','CMOS56:RESOLVER_IN','SYSTEM_TEST','TEST','SPATIAL','ACTIVE','NEW','Resolver point in','test',2,'{}',NULL);

INSERT INTO geo_entity_resolutions(
  entity_type,entity_id,status,match_type,confidence,resolved_label,geom,spatial_precision
) VALUES(
  'ALERT','56000000-0000-0000-0000-000000000016','RESOLVED','LOCAL_COUNTY_CENTROID',0.2,
  'Resolver test point',ST_SetSRID(ST_MakePoint(-74.0200,40.7605),4326),'APPROXIMATE_COUNTY'
);

SELECT count(*)=1 FROM gis_active_spatial_watch_matches('CMOS56:IN');
SELECT count(*)=0 FROM gis_active_spatial_watch_matches('CMOS56:OUT');
SELECT count(*)=0 FROM gis_active_spatial_watch_matches('CMOS56:UNRESOLVED');
SELECT count(*)=1 FROM gis_active_spatial_watch_matches(
  'CMOS56:UNRESOLVED',ST_SetSRID(ST_MakePoint(-74.0200,40.7605),4326)
);
SELECT count(*)=1 FROM gis_active_spatial_watch_matches('CMOS56:RESOLVER_IN');
SELECT match_reason LIKE '%resolver point (APPROXIMATE_COUNTY)%'
FROM gis_active_spatial_watch_matches('CMOS56:RESOLVER_IN');
SELECT match_type='PROXIMITY' AND distance_ft>0 AND distance_ft<500
FROM gis_active_spatial_watch_matches('CMOS56:IN');
SELECT spatial_target_geom IS NOT NULL AND spatial_geom IS NOT NULL
FROM watch_items WHERE watch_id='CMOS56_IN';
SELECT count(*)=1 FROM gis_active_spatial_watch_matches('CMOS56:CORRIDOR_IN');
SELECT count(*)=0 FROM gis_active_spatial_watch_matches('CMOS56:CORRIDOR_OUT');
SELECT ST_GeometryType(spatial_target_geom)='ST_LineString' AND spatial_geom IS NOT NULL
FROM watch_items WHERE watch_id='CMOS56_CORRIDOR';
ROLLBACK;
SQL
)"
[[ "$TEST_STATE" == $'t\nt\nt\nt\nt\nt\nt\nt\nt\nt\nt' ]] || fail "spatial behavior test failed: ${TEST_STATE}"

log "Verifying functions, trigger, grants, and existing spatial watches"
STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT EXISTS (
  SELECT 1 FROM information_schema.columns
  WHERE table_schema='public' AND table_name='watch_items' AND column_name='spatial_target_geom'
);
SELECT EXISTS (
  SELECT 1 FROM pg_trigger
  WHERE tgrelid='public.watch_items'::regclass AND tgname='trg_gis_prepare_spatial_watch' AND NOT tgisinternal
);
SELECT count(DISTINCT p.proname)=3
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname IN (
  'gis_prepare_spatial_watch','gis_active_spatial_watch_matches','gis_spatial_history'
);
SELECT NOT EXISTS (
  SELECT 1 FROM watch_items
  WHERE nearby_enabled AND coalesce(spatial_target_geom,geom) IS NOT NULL AND spatial_geom IS NULL
);
SET ROLE citymanager_app;
SELECT count(*)>=0 FROM gis_active_spatial_watch_matches('__CMOS56_NOT_FOUND__');
SELECT count(*)>=0 FROM gis_active_spatial_watch_matches(
  '__CMOS56_NOT_FOUND__',ST_SetSRID(ST_MakePoint(-74.02,40.76),4326)
);
SELECT gis_spatial_history(ST_SetSRID(ST_MakePoint(-74.02,40.76),4326),500,interval '1 hour',NULL,NULL,1) IS NOT NULL;
RESET ROLE;
SQL
)"
[[ "$STATE" == $'t\nt\nt\nt\nt\nt\nt' ]] || fail "unexpected verification state: ${STATE}"

log "#56 DATABASE INSTALL: PASS"
log "Schema backup: ${SCHEMA_BACKUP}"
log "Watch/routing backup: ${WATCH_BACKUP}"
log "No synthetic alerts, watches, routes, deliveries, or notifications were retained"
