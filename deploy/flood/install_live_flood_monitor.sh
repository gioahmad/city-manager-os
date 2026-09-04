#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
WORKFLOW_ID="FloodLiveMon01"
WORKFLOW_FILE="$REPO/workflows/live/FLOOD_live.json"
BACKUP_ROOT="/var/backups/city-manager-os/flood-live"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$RUN_ID"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "LIVE FLOOD INTELLIGENCE FAILED with exit code ${rc}. Existing static FEMA data was not modified."; exit $rc' ERR

for cmd in git docker python3 curl; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
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

[[ -s "$WORKFLOW_FILE" ]] || fail "Flood workflow JSON is missing"
python3 -m json.tool "$WORKFLOW_FILE" >/dev/null
log "Flood workflow JSON passed syntax validation"

log "Checking authoritative NOAA and NWS endpoints"
python3 - <<'PY'
import json, urllib.request

def get(url, accept='application/json'):
    req=urllib.request.Request(url,headers={'Accept':accept,'User-Agent':'CityManagerOS/1.0 Weehawken-Township'})
    with urllib.request.urlopen(req,timeout=25) as r:
        return json.load(r)

th=get('https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/8518750/floodlevels.json?units=english')
wl=get('https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?date=latest&station=8518750&product=water_level&datum=MHHW&time_zone=gmt&units=english&application=CityManagerOS&format=json')
nws=get('https://api.weather.gov/alerts/active?point=40.7690,-74.0200','application/geo+json')
if not isinstance(wl.get('data'),list) or not wl['data']:
    raise SystemExit('NOAA latest water-level response has no data')
if not isinstance(nws.get('features'),list):
    raise SystemExit('NWS alert response is not a FeatureCollection')
print('NOAA_LATEST_WATER_LEVEL='+str(wl['data'][-1].get('v')))
print('NWS_ACTIVE_FEATURES='+str(len(nws['features'])))
print('NOAA_THRESHOLDS_RESPONSE=OK')
PY

log "Verifying existing static flood data before live cutover"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT count(*) AS flood_zones,
       count(*) FILTER (WHERE sfha_tf='T') AS sfha,
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_geom
FROM gis_flood_zones;
SELECT count(*) AS watched_locations_in_flood_zones FROM gis_watch_items_in_flood_zones();
SQL

log "Creating dynamic central-routing source watches for live flood alerts"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
INSERT INTO watch_items(
  watch_id,active,watch_type,display_name,search_term,aliases,
  match_mode,match_field,category,tags,source_filter,
  alert_category_filter,min_priority,gis_enabled,nearby_enabled,
  source_notes,notes,updated_at
)
VALUES
  ('CMOS_FLOOD_NOAA',true,'SOURCE','NOAA Tide / Flood Intelligence','NOAA_TIDE',ARRAY['NOAA FLOOD','TIDE','WATER LEVEL'],'FIELD','source','OEM',ARRAY['flood','noaa','tide'],ARRAY[]::text[],ARRAY['OEM'],2,false,false,'City Manager OS live flood source','Routes NOAA threshold changes through central matcher',now()),
  ('CMOS_FLOOD_NWS',true,'SOURCE','NWS Flood Intelligence','NWS_FLOOD',ARRAY['NWS FLOOD','COASTAL FLOOD'],'FIELD','source','OEM',ARRAY['flood','nws'],ARRAY[]::text[],ARRAY['OEM'],2,false,false,'City Manager OS live flood source','Routes official NWS flood alerts through central matcher',now())
ON CONFLICT(watch_id) DO UPDATE SET
  active=EXCLUDED.active,
  watch_type=EXCLUDED.watch_type,
  display_name=EXCLUDED.display_name,
  search_term=EXCLUDED.search_term,
  aliases=EXCLUDED.aliases,
  match_mode=EXCLUDED.match_mode,
  match_field=EXCLUDED.match_field,
  category=EXCLUDED.category,
  tags=EXCLUDED.tags,
  source_filter=EXCLUDED.source_filter,
  alert_category_filter=EXCLUDED.alert_category_filter,
  min_priority=EXCLUDED.min_priority,
  source_notes=EXCLUDED.source_notes,
  notes=EXCLUDED.notes,
  updated_at=now();

INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
SELECT w.id,s.id,true
FROM watch_items w
JOIN subscribers s ON s.subscriber_id='GIO_CATCHALL' AND s.active=true
WHERE w.watch_id IN ('CMOS_FLOOD_NOAA','CMOS_FLOOD_NWS')
ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true;

SELECT w.watch_id,w.search_term,w.match_mode,w.match_field,s.subscriber_id,
       CASE WHEN NULLIF(s.ntfy_topic,'') IS NULL THEN 'MISSING' ELSE 'DYNAMIC_TOPIC_PRESENT' END AS destination
FROM watch_items w
JOIN watch_item_recipients r ON r.watch_item_id=w.id AND r.active=true
JOIN subscribers s ON s.id=r.subscriber_id AND s.active=true
WHERE w.watch_id IN ('CMOS_FLOOD_NOAA','CMOS_FLOOD_NWS')
ORDER BY w.watch_id;
SQL

log "Locating n8n database and making non-destructive backup"
N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
[[ -n "$N8N_DIR" && -f "$N8N_DIR/database.sqlite" ]] || fail "Could not locate n8n database.sqlite"
mkdir -p "$BACKUP_DIR"
cp -a "$N8N_DIR/database.sqlite" "$BACKUP_DIR/database.sqlite"
for suffix in -wal -shm; do
  [[ -f "$N8N_DIR/database.sqlite${suffix}" ]] && cp -a "$N8N_DIR/database.sqlite${suffix}" "$BACKUP_DIR/database.sqlite${suffix}"
done
cp -a "$WORKFLOW_FILE" "$BACKUP_DIR/FLOOD_live.json"
log "n8n backup: $BACKUP_DIR"

log "Copying live flood workflow into isolated n8n container"
docker cp "$WORKFLOW_FILE" n8n:/tmp/FLOOD_live.json

log "Importing flood workflow with n8n CLI"
docker exec -u node n8n n8n import:workflow --input=/tmp/FLOOD_live.json

log "Publishing flood workflow"
if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n publish:workflow --id="$WORKFLOW_ID"
else
  docker exec -u node n8n n8n update:workflow --id="$WORKFLOW_ID" --active=true
fi

log "Restarting n8n so trigger publication is loaded"
docker restart n8n >/dev/null
for i in $(seq 1 60); do
  STATE="$(docker inspect n8n --format '{{.State.Status}}' 2>/dev/null || true)"
  [[ "$STATE" == "running" ]] && break
  sleep 2
done
[[ "$(docker inspect n8n --format '{{.State.Status}}')" == "running" ]] || fail "n8n did not return to running state"
sleep 5

log "Verifying published flood workflow in n8n database"
python3 - "$N8N_DIR/database.sqlite" "$WORKFLOW_ID" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
r=con.execute('SELECT id,name,active,nodes,activeVersionId,versionCounter FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not r:
    raise SystemExit('Flood workflow missing after import')
nodes=json.loads(r['nodes'])
names={n.get('name') for n in nodes}
required={'Flood Monitor Schedule','Fetch NOAA Flood Thresholds','Fetch NOAA Water Level','Fetch NWS Active Alerts','Build Standard Flood Alerts','Send Flood Alert to Central Watchlist Matcher'}
missing=sorted(required-names)
if missing:
    raise SystemExit('Flood workflow missing nodes: '+', '.join(missing))
if not r['active']:
    raise SystemExit('Flood workflow is not active/published')
print(f"FLOOD_WORKFLOW active={r['active']} version={r['versionCounter']} activeVersionId={r['activeVersionId']}")
con.close()
PY

log "Waiting for the published 5-minute schedule to record its first NOAA observation"
OBS_COUNT=0
for i in $(seq 1 75); do
  OBS_COUNT="$(docker exec -i citymanager-postgis sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM flood_observations WHERE source='"'"'NOAA_TIDE'"'"' AND station_id='"'"'8518750'"'"';"' 2>/dev/null || echo 0)"
  if [[ "${OBS_COUNT:-0}" =~ ^[0-9]+$ ]] && (( OBS_COUNT > 0 )); then
    log "Scheduled NOAA observation recorded"
    break
  fi
  if (( i % 12 == 0 )); then
    log "Still waiting for scheduled flood workflow..."
  fi
  sleep 5
done
[[ "${OBS_COUNT:-0}" =~ ^[0-9]+$ ]] && (( OBS_COUNT > 0 )) || fail "Published flood workflow did not record a NOAA observation within the validation window"

log "Verifying live observations and source health"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT source,station_id,observed_at,water_level_mhhw_ft,flood_category,title
FROM flood_observations
WHERE source='NOAA_TIDE' AND station_id='8518750'
ORDER BY observed_at DESC
LIMIT 3;

SELECT source_id,status,last_attempt_at,last_success_at,last_event_at,last_error,metadata
FROM source_health
WHERE source_id IN ('NOAA_TIDE','NWS_FLOOD')
ORDER BY source_id;

SELECT source,count(*) AS alerts,max(received_at) AS latest_alert
FROM alerts
WHERE source IN ('NOAA_TIDE','NWS_FLOOD')
GROUP BY source
ORDER BY source;
SQL

log "Running complete City Manager OS health check"
cd "$REPO"
./deploy/cmos-health

log "LIVE FLOOD INTELLIGENCE PASSED"
log "Workflow: ${WORKFLOW_ID} · every 5 minutes"
log "NOAA: station 8518750 The Battery · observed water level relative to MHHW"
log "NWS: active official flood/coastal alerts for the Weehawken point"
log "Routing: existing central watchlist matcher · dynamic subscriber destination"
log "Flood dashboard: http://100.94.203.47:8090/flood"
log "Repository commit: ${HEAD_SHA}"
