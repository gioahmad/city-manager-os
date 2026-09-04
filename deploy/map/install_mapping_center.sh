#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "MAPPING CENTER DEPLOYMENT FAILED with exit code ${rc}."; exit $rc' ERR

for cmd in git docker python3; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
cd "$REPO"

if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean"
fi

log "Synchronizing origin/main"
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ ${HEAD_SHA}"

log "Syntax-checking web GIS modules"
python3 -m py_compile dashboard/map_app.py dashboard/flood_app.py dashboard/phase3_app.py

log "Applying Mapping Center schema and web-role grants"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/014_mapping_center.sql

log "Verifying map storage, FEMA data, and citymanager_app access"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'MAP_LAYERS' AS check,count(*) AS rows FROM map_layers;
SELECT 'FEMA' AS check,count(*) AS flood_zones,
       count(*) FILTER (WHERE sfha_tf='T') AS sfha,
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_geom
FROM gis_flood_zones;
SET ROLE citymanager_app;
SELECT 'WEB_ROLE_MAP_ACCESS' AS check,
       (SELECT count(*) FROM map_layers) AS layers,
       (SELECT count(*) FROM map_features) AS features;
RESET ROLE;
SQL

log "Building shared dashboard web image"
cd "$REPO/dashboard"
docker compose build citymanager-dashboard

log "Recreating private dashboard and employee web portal only"
docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-staff

wait_inside(){
  local container="$1" url="$2" label="$3"
  for i in $(seq 1 60); do
    if docker exec "$container" python -c "import urllib.request; urllib.request.urlopen('${url}',timeout=4).read()" >/dev/null 2>&1; then
      log "${label} ready"
      return 0
    fi
    state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
    if [[ "$state" == "exited" || "$state" == "dead" ]]; then
      docker logs --tail 120 "$container" || true
      fail "${label} container stopped during startup"
    fi
    sleep 2
  done
  docker inspect "$container" --format 'status={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} error={{.State.Error}}' || true
  docker logs --tail 120 "$container" || true
  fail "${label} did not become ready"
}

check_page(){
  local container="$1" url="$2" label="$3"
  if ! docker exec "$container" python -c "import urllib.request; assert urllib.request.urlopen('${url}',timeout=20).status==200"; then
    log "${label} failed; dashboard exception follows"
    docker logs --tail 160 "$container" || true
    fail "${label} returned an error"
  fi
  log "${label} passed"
}

log "Waiting for private dashboard"
wait_inside citymanager-dashboard http://127.0.0.1:8000/health "Private dashboard"

log "Checking Mapping Center and Flood Intelligence pages inside container"
check_page citymanager-dashboard http://127.0.0.1:8000/map "Mapping Center page"
check_page citymanager-dashboard http://127.0.0.1:8000/flood "Flood Intelligence page"
if ! docker exec citymanager-dashboard python -c "import urllib.request,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/map/system/flood.geojson',timeout=30)); assert d.get('type')=='FeatureCollection' and len(d.get('features',[]))>0; print('LOCAL_FLOOD_FEATURES='+str(len(d['features']))"; then
  docker logs --tail 160 citymanager-dashboard || true
  fail "Local flood GeoJSON endpoint failed"
fi

log "Waiting for employee portal"
wait_inside citymanager-staff http://127.0.0.1:8000/staff "Employee portal"

docker exec citymanager-dashboard test -f /app/static/photo_viewer.js
docker exec citymanager-staff test -f /app/static/photo_viewer.js

cd "$REPO"
./deploy/cmos-health

log "MAPPING CENTER PASSED"
log "Mapping Center: http://100.94.203.47:8090/map"
log "Flood Intelligence: http://100.94.203.47:8090/flood"
log "Operations photos now open inline with zoom; download remains explicit."
log "Repository commit: ${HEAD_SHA}"
