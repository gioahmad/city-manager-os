#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
DATA_DIR="/opt/citymanager-data/gis/raw/flood"
FEMA_FILE="$DATA_DIR/weehawken_fema_nfhl.geojson"
LOG_PREFIX="STATIC FLOOD INTELLIGENCE"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "${LOG_PREFIX} FAILED with exit code ${rc}. Existing production flood data was retained unless the atomic database transaction had already committed."; exit $rc' ERR

for cmd in git docker python3 ogr2ogr ogrinfo curl; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
cd "$REPO"

if [[ -n "$(git status --porcelain)" ]]; then git status --short; fail "Repository is not clean"; fi
log "Synchronizing origin/main"
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ $HEAD_SHA"

log "Applying idempotent flood intelligence schema"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/013_flood_intelligence.sql

log "Calculating FEMA query bounds from local Weehawken parcel geometry"
BBOX="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
WITH e AS (
  SELECT ST_Expand(ST_Extent(geom),0.01) AS b
  FROM gis_parcels
  WHERE lower(trim(mun_name))='weehawken'
)
SELECT ST_XMin(b)||','||ST_YMin(b)||','||ST_XMax(b)||','||ST_YMax(b) FROM e WHERE b IS NOT NULL;
SQL
)"
[[ -n "$BBOX" ]] || fail "Could not calculate Weehawken parcel bounds"
log "FEMA bbox: $BBOX"

mkdir -p "$DATA_DIR"
TMP_FILE="${FEMA_FILE}.new.$(date +%s)"
log "Downloading effective FEMA NFHL flood zones"
python3 deploy/flood/download_fema_nfhl.py --bbox "$BBOX" --output "$TMP_FILE"
[[ -s "$TMP_FILE" ]] || fail "FEMA download is empty"
FEATURES="$(ogrinfo -ro -so -al "$TMP_FILE" 2>/dev/null | awk -F': ' '/Feature Count:/ {print $2; exit}')"
[[ "${FEATURES:-0}" =~ ^[0-9]+$ ]] || fail "Unable to read FEMA feature count"
(( FEATURES > 0 )) || fail "FEMA returned zero features"
log "FEMA source features: $FEATURES"

log "Importing FEMA data into isolated staging table"
POSTGIS_ENV="$REPO/deploy/postgis/.env"
[[ -f "$POSTGIS_ENV" ]] || fail "PostGIS .env missing"
set -a; source "$POSTGIS_ENV"; set +a
PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}' citymanager-postgis | awk '{print $1}')"
[[ -n "$PG_IP" ]] || fail "Unable to resolve PostGIS IP"
PG_DSN="PG:host=${PG_IP} port=5432 dbname=${POSTGRES_DB} user=${POSTGRES_USER}"
PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr -f PostgreSQL "$PG_DSN" "$TMP_FILE" \
  -nln public.stg_weehawken_flood_zones -overwrite -t_srs EPSG:4326 \
  -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom -lco SPATIAL_INDEX=GIST \
  --config PG_USE_COPY YES

log "Checking FEMA staging schema"
MISSING="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
WITH required(c) AS (VALUES ('geom'),('dfirm_id'),('fld_zone'),('zone_subty'),('sfha_tf'),('static_bfe'),('depth'),('velocity'),('objectid'))
SELECT c FROM required r LEFT JOIN information_schema.columns i
  ON i.table_schema='public' AND i.table_name='stg_weehawken_flood_zones' AND i.column_name=r.c
WHERE i.column_name IS NULL ORDER BY c;
SQL
)"
if [[ -n "$MISSING" ]]; then printf '%s\n' "$MISSING"; fail "FEMA source schema changed; production not touched"; fi

log "Validating and atomically promoting FEMA flood zones"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
BEGIN;
CREATE TEMP TABLE flood_candidate ON COMMIT DROP AS
SELECT
  dfirm_id::text,
  fld_zone::text,
  zone_subty::text,
  sfha_tf::text,
  NULLIF(static_bfe::text,'')::double precision AS static_bfe,
  NULLIF(depth::text,'')::double precision AS depth,
  NULLIF(velocity::text,'')::double precision AS velocity,
  objectid::bigint AS source_objectid,
  ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))::geometry(MultiPolygon,4326) AS geom
FROM stg_weehawken_flood_zones
WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom);

DO $$
DECLARE n bigint; bad bigint;
BEGIN
  SELECT count(*), count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) INTO n,bad FROM flood_candidate;
  IF n=0 THEN RAISE EXCEPTION 'Flood candidate is empty'; END IF;
  IF bad<>0 THEN RAISE EXCEPTION 'Flood candidate has % bad geometries',bad; END IF;
END $$;

TRUNCATE gis_flood_zones RESTART IDENTITY;
INSERT INTO gis_flood_zones(dfirm_id,fld_zone,zone_subty,sfha_tf,static_bfe,depth,velocity,source_objectid,geom,imported_at)
SELECT dfirm_id,fld_zone,zone_subty,sfha_tf,static_bfe,depth,velocity,source_objectid,geom,now() FROM flood_candidate;
COMMIT;

ANALYZE gis_flood_zones;
SELECT 'FLOOD_ZONES' AS check,count(*) AS rows,
       count(*) FILTER (WHERE sfha_tf='T') AS sfha,
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_geom
FROM gis_flood_zones;
SELECT 'WATCHED_IN_FLOOD' AS check,count(*) FROM gis_watch_items_in_flood_zones();
SQL

log "Recording FEMA dataset version and health"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
INSERT INTO gis_dataset_versions(dataset_id,dataset_name,source_url,imported_at,row_count,status,notes)
SELECT 'FEMA_NFHL_WEEHAWKEN','FEMA NFHL Flood Hazard Zones - Weehawken Area',
'https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28',now(),count(*),'ACTIVE','Local effective NFHL import; repository ${HEAD_SHA}'
FROM gis_flood_zones
ON CONFLICT(dataset_id) DO UPDATE SET imported_at=EXCLUDED.imported_at,row_count=EXCLUDED.row_count,status=EXCLUDED.status,notes=EXCLUDED.notes,source_url=EXCLUDED.source_url,dataset_name=EXCLUDED.dataset_name;
INSERT INTO source_health(source_id,status,last_attempt_at,last_success_at,last_error,metadata,updated_at)
VALUES('FEMA_FLOOD','OK',now(),now(),NULL,jsonb_build_object('rows',(SELECT count(*) FROM gis_flood_zones),'bbox','${BBOX}'),now())
ON CONFLICT(source_id) DO UPDATE SET status='OK',last_attempt_at=now(),last_success_at=now(),last_error=NULL,metadata=EXCLUDED.metadata,updated_at=now();
SQL

mv -f "$TMP_FILE" "$FEMA_FILE"
META_TMP="${TMP_FILE%.geojson}.metadata.json"
[[ -f "$META_TMP" ]] && mv -f "$META_TMP" "${FEMA_FILE%.geojson}.metadata.json" || true

log "Building dashboard image with local flood view"
cd "$REPO/dashboard"
docker compose build citymanager-dashboard
python3 -m py_compile flood_app.py phase3_app.py
log "Recreating private dashboard only"
docker compose up -d --no-deps --force-recreate citymanager-dashboard

log "Waiting for private dashboard health"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8090/health >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:8090/health >/dev/null
curl -fsS http://127.0.0.1:8090/flood >/dev/null

cd "$REPO"
./deploy/cmos-health
log "STATIC FLOOD INTELLIGENCE PASSED"
log "Flood page: http://100.94.203.47:8090/flood"
log "Repository commit: ${HEAD_SHA}"
log "Next: live NOAA/NWS flood source through the existing n8n central matcher."
