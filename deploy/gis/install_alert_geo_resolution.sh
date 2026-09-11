#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "ALERT GEO RESOLUTION INSTALL FAILED with exit code ${rc}."; exit $rc' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"

log "Applying additive alert geography migration"
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < deploy/postgis/init/030_alert_geo_resolution.sql

STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq' <<'SQL'
SELECT to_regclass('public.geo_resolution_cache') IS NOT NULL;
SELECT to_regclass('public.geo_entity_resolutions') IS NOT NULL;
SELECT to_regclass('public.alert_geo_coverage') IS NOT NULL;
SELECT count(*) FROM gis_parcels;
SELECT count(*) FROM gis_addresses;
SQL
)"
[[ "$STATE" == $'t\nt\nt\n3481240\n3755307' ]] || fail "unexpected post-migration state: ${STATE}"

log "ALERT GEO RESOLUTION INSTALL: PASS"
log "No alert history was changed and no backfill was started by this installer."
