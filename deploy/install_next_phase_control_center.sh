#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
TARGET_BRANCH="${CMOS_DEPLOY_BRANCH:-main}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="/var/backups/city-manager-os/next-phase/${STAMP}"
DASHBOARD_IMAGE="dashboard-citymanager-dashboard:latest"
ROLLBACK_IMAGE="citymanager-dashboard-pre-nextphase:${STAMP}"
TEST_CONTAINER="citymanager-dashboard-nextphase-test"
STATUS="FAIL"
PRODUCTION_RECREATED=0
N8N_TOUCHED=0
N8N_DIR=""
N8N_DB=""
N8N_UID=""
N8N_GID=""
N8N_MODE=""

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

finish(){
  rc=$?
  set +e
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true

  if [[ "$STATUS" != "PASS" ]]; then
    log "Deployment did not complete. Starting automatic service rollback where possible."

    if [[ "$N8N_TOUCHED" == "1" && -n "$N8N_DB" && -s "${BACKUP_DIR}/n8n-database.sqlite" ]]; then
      log "Restoring pre-deployment n8n SQLite database"
      docker stop n8n >/dev/null 2>&1 || true
      rm -f "${N8N_DB}-wal" "${N8N_DB}-shm" 2>/dev/null || true
      cp "${BACKUP_DIR}/n8n-database.sqlite" "$N8N_DB" || true
      [[ -n "$N8N_UID" && -n "$N8N_GID" ]] && chown "${N8N_UID}:${N8N_GID}" "$N8N_DB" 2>/dev/null || true
      [[ -n "$N8N_MODE" ]] && chmod "$N8N_MODE" "$N8N_DB" 2>/dev/null || true
      docker start n8n >/dev/null 2>&1 || true
    fi

    if [[ "$PRODUCTION_RECREATED" == "1" ]] && docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
      log "Restoring prior City Manager OS dashboard image"
      docker tag "$ROLLBACK_IMAGE" "$DASHBOARD_IMAGE" || true
      docker rm -f citymanager-integration-engine >/dev/null 2>&1 || true
      cd "$REPO/dashboard" || true
      docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-staff citymanager-ops-engine >/dev/null 2>&1 || true
      cd "$REPO" || true
    fi

    log "Backups retained at ${BACKUP_DIR}"
    echo "COMPLETE: FAIL"
  else
    echo "COMPLETE: PASS"
  fi
  exit "$rc"
}
trap finish EXIT

for cmd in git docker python3; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
cd "$REPO"

if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean"
fi

log "Synchronizing origin/${TARGET_BRANCH}"
git fetch origin "${TARGET_BRANCH}"
git switch "${TARGET_BRANCH}"
git pull --ff-only origin "${TARGET_BRANCH}"
HEAD_SHA="$(git rev-parse HEAD)"
log "Using ${TARGET_BRANCH} @ ${HEAD_SHA}"

for c in citymanager-postgis citymanager-dashboard n8n; do
  [[ "$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || true)" == "running" ]] || fail "Required container not running: $c"
done

docker image inspect "$DASHBOARD_IMAGE" >/dev/null 2>&1 || fail "Current dashboard image not found: $DASHBOARD_IMAGE"
docker tag "$DASHBOARD_IMAGE" "$ROLLBACK_IMAGE"
log "Tagged rollback image ${ROLLBACK_IMAGE}"

mkdir -p "$BACKUP_DIR"
log "Backing up PostgreSQL before schema changes"
docker exec citymanager-postgis sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "${BACKUP_DIR}/citymanager.dump"
[[ -s "${BACKUP_DIR}/citymanager.dump" ]] || fail "PostgreSQL backup is empty"

log "Backing up n8n SQLite database"
N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="${N8N_DIR}/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database not found"
N8N_UID="$(stat -c '%u' "$N8N_DB")"
N8N_GID="$(stat -c '%g' "$N8N_DB")"
N8N_MODE="$(stat -c '%a' "$N8N_DB")"
python3 - "$N8N_DB" "${BACKUP_DIR}/n8n-database.sqlite" <<'PY'
import sqlite3,sys
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
[[ -s "${BACKUP_DIR}/n8n-database.sqlite" ]] || fail "n8n backup is empty"
cp -a workflows/sources/EVENT_INTELLIGENCE_Alerts_v1.json "${BACKUP_DIR}/" || true
log "Backups stored at ${BACKUP_DIR}"

log "Syntax-checking next-phase Python modules"
python3 -m py_compile \
  dashboard/integration_runtime.py \
  dashboard/integration_engine.py \
  dashboard/integration_worker.py \
  dashboard/integrations_app.py \
  dashboard/phase3_app.py

log "Validating n8n workflow JSON"
python3 -m json.tool workflows/sources/EVENT_INTELLIGENCE_Alerts_v1.json >/dev/null

log "Applying additive database migrations"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/015_next_phase_control_center.sql
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/016_event_intelligence_watch.sql

log "Verifying database objects and preserved issue source of truth"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'ISSUES_SOURCE_OF_TRUTH' AS check,to_regclass('public.issues') AS relation;
SELECT 'INTEGRATIONS' AS check,count(*) AS rows FROM integrations;
SELECT 'EVENT_INTELLIGENCE' AS check,count(*) AS rows FROM event_intelligence;
SELECT 'EVENT_WATCH' AS check,count(*) AS rows FROM watch_items WHERE watch_id='CMOS_EVENT_INTELLIGENCE_HIGH';
SELECT 'NYC_SOURCE' AS check,integration_key,active,parser_kind,poll_seconds FROM integrations WHERE integration_key='NYC_PERMITTED_EVENTS';
SET ROLE citymanager_app;
SELECT 'WEB_ROLE_INTEGRATIONS' AS check,count(*) FROM integrations;
SELECT 'WEB_ROLE_EVENT_INTEL' AS check,count(*) FROM event_intelligence;
RESET ROLE;
SQL

log "Building candidate dashboard image"
cd "$REPO/dashboard"
docker compose build citymanager-dashboard
cd "$REPO"

check_page(){
  local container="$1" url="$2" label="$3"
  if ! docker exec "$container" python -c "import urllib.request; r=urllib.request.urlopen('${url}',timeout=20); assert r.status==200"; then
    docker logs --tail 180 "$container" || true
    fail "${label} returned an error"
  fi
  log "${label} passed"
}

log "Starting temporary preflight dashboard container"
docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
docker run -d \
  --name "$TEST_CONTAINER" \
  --network citymanager \
  --env-file "$REPO/dashboard/.env" \
  -e DB_HOST=citymanager-postgis \
  -e DB_PORT=5432 \
  -e DB_NAME=citymanager \
  -e DB_USER=citymanager_app \
  -e TZ="${APP_TIMEZONE:-America/New_York}" \
  -e PGTZ="${APP_TIMEZONE:-America/New_York}" \
  -e API_LAB_ALLOW_PRIVATE=false \
  "$DASHBOARD_IMAGE" >/dev/null

preflight_ready=0
for _ in $(seq 1 40); do
  if docker exec "$TEST_CONTAINER" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).read()" >/dev/null 2>&1; then
    preflight_ready=1
    break
  fi
  state="$(docker inspect "$TEST_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)"
  if [[ "$state" == "exited" || "$state" == "dead" ]]; then
    docker logs --tail 180 "$TEST_CONTAINER" || true
    fail "Preflight dashboard container stopped during startup"
  fi
  sleep 2
done
(( preflight_ready == 1 )) || {
  docker logs --tail 180 "$TEST_CONTAINER" || true
  fail "Preflight dashboard did not become ready"
}

log "Smoke-testing old and new routes before replacing production"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/ "Preflight Overview"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/issues "Preflight Command Center"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/schedule "Preflight Schedule"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/watchlist "Preflight Watchlists"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/subscribers "Preflight Subscribers"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/routing "Preflight Routing"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/modules "Preflight Modules"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/admin-tools "Preflight Admin Tools"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/alert-admin "Preflight Alert Admin"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/integrations "Preflight Integrations Center"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/api-lab "Preflight API Lab"
check_page "$TEST_CONTAINER" http://127.0.0.1:8000/event-intelligence "Preflight Event Intelligence"

log "Running authoritative NYC permitted-events collector from preflight image"
docker exec -i "$TEST_CONTAINER" python - <<'PY'
from integration_engine import load_integration, run_integration
integration=load_integration(integration_key='NYC_PERMITTED_EVENTS')
outcome=run_integration(integration,run_type='MANUAL',parse_and_store=True)
if not outcome.get('ok'):
    raise SystemExit('NYC event integration failed: '+str(outcome.get('error')))
count=len(outcome.get('events') or [])
print(f'NYC_EVENT_ITEMS={count} CHANGED={outcome.get("changed",0)}')
if count < 1:
    raise SystemExit('NYC event integration returned zero items')
PY

docker rm -f "$TEST_CONTAINER" >/dev/null

log "Verifying event intelligence rows after preflight collector"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'EVENT_INTEL_ROWS' AS check,count(*) AS rows FROM event_intelligence;
SELECT 'EVENT_LEVELS' AS check,impact_level,count(*) FROM event_intelligence GROUP BY impact_level ORDER BY impact_level;
SQL

log "Preflight passed. Recreating production dashboard services"
cd "$REPO/dashboard"
docker compose up -d --no-deps --force-recreate \
  citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine
cd "$REPO"
PRODUCTION_RECREATED=1

wait_inside(){
  local container="$1" url="$2" label="$3"
  for _ in $(seq 1 60); do
    if docker exec "$container" python -c "import urllib.request; urllib.request.urlopen('${url}',timeout=4).read()" >/dev/null 2>&1; then
      log "${label} ready"
      return 0
    fi
    state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
    if [[ "$state" == "exited" || "$state" == "dead" ]]; then
      docker logs --tail 160 "$container" || true
      fail "${label} container stopped during startup"
    fi
    sleep 2
  done
  docker logs --tail 160 "$container" || true
  fail "${label} did not become ready"
}

wait_inside citymanager-dashboard http://127.0.0.1:8000/health "Private dashboard"
wait_inside citymanager-staff http://127.0.0.1:8000/staff "Employee portal"

for _ in $(seq 1 30); do
  state="$(docker inspect citymanager-integration-engine --format '{{.State.Status}}' 2>/dev/null || true)"
  [[ "$state" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect citymanager-integration-engine --format '{{.State.Status}}' 2>/dev/null || true)" == "running" ]] || {
  docker logs --tail 160 citymanager-integration-engine || true
  fail "Integration engine is not running"
}

log "Checking production next-phase pages"
check_page citymanager-dashboard http://127.0.0.1:8000/admin-tools "Admin Tools"
check_page citymanager-dashboard http://127.0.0.1:8000/integrations "Integrations Center"
check_page citymanager-dashboard http://127.0.0.1:8000/api-lab "API Lab"
check_page citymanager-dashboard http://127.0.0.1:8000/event-intelligence "Event Intelligence"

log "Importing event-intelligence alert workflow into n8n"
N8N_TOUCHED=1
docker cp workflows/sources/EVENT_INTELLIGENCE_Alerts_v1.json n8n:/tmp/EVENT_INTELLIGENCE_Alerts_v1.json
docker exec -u node n8n n8n import:workflow --input=/tmp/EVENT_INTELLIGENCE_Alerts_v1.json >/dev/null || fail "n8n workflow import failed"
if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n publish:workflow --id="EvtIntelAlertsV1" >/dev/null || fail "event workflow publish failed"
else
  docker exec -u node n8n n8n update:workflow --id="EvtIntelAlertsV1" --active=true >/dev/null || fail "event workflow activation failed"
fi

docker restart n8n >/dev/null
ready=0
for _ in $(seq 1 60); do
  if docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
(( ready == 1 )) || fail "n8n did not become ready"

log "Verifying event workflow is active and central matcher remains present"
python3 - "$N8N_DB" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
for wid,label in [('EvtIntelAlertsV1','event intelligence'),('ESH9c2pZ8QfkMosO','central matcher')]:
    r=con.execute('SELECT id,name,active,activeVersionId FROM workflow_entity WHERE id=?',(wid,)).fetchone()
    if not r:
        raise SystemExit(f'{label} workflow missing')
    if not r['active'] or not r['activeVersionId']:
        raise SystemExit(f'{label} workflow is not active/published')
    print(f'{label.upper().replace(" ","_")}_ACTIVE=1')
con.close()
PY

log "Running existing City Manager OS health check"
./deploy/cmos-health

log "Running existing end-to-end acceptance suite"
./deploy/cmos-e2e-acceptance

log "Deployment verification complete"
log "Admin Tools: http://100.94.203.47:8090/admin-tools"
log "Integrations: http://100.94.203.47:8090/integrations"
log "API Lab: http://100.94.203.47:8090/api-lab"
log "Event Intelligence: http://100.94.203.47:8090/event-intelligence"
log "Backup: ${BACKUP_DIR}"
log "Rollback image: ${ROLLBACK_IMAGE}"
log "Repository commit: ${HEAD_SHA}"
STATUS="PASS"
