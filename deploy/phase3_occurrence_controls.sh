#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase3/operations-engine"
BACKUP_ROOT="/var/backups/city-manager-os/phase3-occurrence-controls"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "PHASE 3 OCCURRENCE CONTROLS FAILED with exit code ${rc}. Review the log before retrying."; exit $rc' ERR

for cmd in git docker curl ss; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done

cd "$REPO"
log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then git status --short; fail "Repository is not clean. No changes were made."; fi
ORIGINAL_BRANCH="$(git branch --show-current)"
ORIGINAL_HEAD="$(git rev-parse HEAD)"
log "Starting from ${ORIGINAL_BRANCH:-detached} @ ${ORIGINAL_HEAD}"

log "Checking current private dashboard and public Operations portal"
docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())"
PUBLIC_BEFORE="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC_BEFORE"
grep -q '"version":3' <<<"$PUBLIC_BEFORE" || fail "Public Operations portal is not version 3 before deployment."

log "Fetching occurrence-controls branch head"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
DEPLOYED_HEAD="$(git rev-parse HEAD)"
log "Deploying ${DEPLOYED_HEAD}"

for file in \
  deploy/postgis/init/013_operations_occurrence_controls.sql \
  dashboard/operations_occurrence_controls.py \
  dashboard/operations_engine_windowed.py \
  dashboard/templates/operations_occurrence_controls.html \
  dashboard/templates/operations_admin_insert.html \
  dashboard/templates/operations_my_day_insert.html; do
  [[ -s "$file" ]] || fail "Missing occurrence-controls file: $file"
done

STAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"
mkdir -p "$BACKUP_DIR"
printf '%s\n' "$ORIGINAL_BRANCH" > "$BACKUP_DIR/original_branch.txt"
printf '%s\n' "$ORIGINAL_HEAD" > "$BACKUP_DIR/original_head.txt"
printf '%s\n' "$DEPLOYED_HEAD" > "$BACKUP_DIR/deployment_head.txt"

log "Backing up PostgreSQL before additive migration"
docker exec citymanager-postgis sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_DIR/citymanager-pre-occurrence-controls.dump"
[[ -s "$BACKUP_DIR/citymanager-pre-occurrence-controls.dump" ]] || fail "Database backup is empty."

cd "$REPO/dashboard"
log "Validating compose configuration"
docker compose config >/tmp/cmos-phase3-occurrence-compose.txt

log "Building shared application image"
docker compose build citymanager-dashboard

log "Compiling occurrence-control modules"
docker run --rm --entrypoint python dashboard-citymanager-dashboard:latest -m py_compile \
  operations_occurrence_controls.py operations_engine_windowed.py operations_engine.py \
  operations_routines_app.py phase3_app.py schedule_app.py staff_admin_app.py

log "Parsing all Jinja templates"
docker run --rm -i --entrypoint python dashboard-citymanager-dashboard:latest - <<'PY'
from pathlib import Path
from jinja2 import Environment
env=Environment()
files=sorted(Path('/app/templates').glob('*.html'))
for path in files:
    env.parse(path.read_text())
print(f"Template parse: OK ({len(files)} templates)")
PY

cd "$REPO"
log "Applying additive occurrence-controls migration"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/013_operations_occurrence_controls.sql

log "Verifying routine window and run-note columns"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL' >/tmp/cmos-phase3-occurrence-db.txt
SELECT count(*) FROM information_schema.columns WHERE table_name='operations_routines' AND column_name IN ('starts_on','ends_on');
SELECT count(*) FROM information_schema.columns WHERE table_name='operations_routine_runs' AND column_name IN ('run_note','run_note_by','run_note_at');
SELECT count(*) FROM pg_constraint WHERE conname='operations_routines_valid_window';
SQL
cat /tmp/cmos-phase3-occurrence-db.txt
grep -q '^2$' /tmp/cmos-phase3-occurrence-db.txt || fail "Routine date-window columns incomplete."
grep -q '^3$' /tmp/cmos-phase3-occurrence-db.txt || fail "Occurrence note columns incomplete."
grep -q '^1$' /tmp/cmos-phase3-occurrence-db.txt || fail "Routine date-window constraint missing."

cd "$REPO/dashboard"
log "Running read-only window-aware worker dry run"
docker run --rm --network citymanager --env-file .env \
  -e DB_HOST=citymanager-postgis -e DB_PORT=5432 -e DB_NAME=citymanager -e DB_USER=citymanager_app \
  -e TZ=America/New_York -e PGTZ=America/New_York \
  dashboard-citymanager-dashboard:latest python operations_engine_windowed.py --dry-run

log "Recreating private dashboard and Operations worker only"
docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-ops-engine

log "Waiting for private dashboard"
DASH_OK=0
for _ in $(seq 1 30); do
  if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-phase3-occurrence-health.txt 2>/dev/null; then DASH_OK=1; break; fi
  sleep 2
done
[[ "$DASH_OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-phase3-occurrence-health.txt

log "Waiting for Operations Engine health"
ENGINE_OK=0
for _ in $(seq 1 30); do
  ENGINE_STATUS="$(docker exec citymanager-postgis sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT status FROM source_health WHERE source_id='\''OPERATIONS_ENGINE'\'';"' 2>/dev/null || true)"
  if [[ "$ENGINE_STATUS" == "OK" ]]; then ENGINE_OK=1; break; fi
  sleep 2
done
[[ "$ENGINE_OK" -eq 1 ]] || { docker logs --tail 120 citymanager-ops-engine || true; fail "Operations Engine did not report OK."; }
echo "OPERATIONS_ENGINE=$ENGINE_STATUS"

docker inspect citymanager-ops-engine --format '{{json .Config.Cmd}}' | tee /tmp/cmos-phase3-occurrence-worker-cmd.txt
grep -q 'operations_engine_windowed.py' /tmp/cmos-phase3-occurrence-worker-cmd.txt || fail "Window-aware worker command is not active."

log "Testing occurrence-controls private route"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
for path, marker in {
    '/operations-routines':'DATE WINDOWS &amp; TODAY NOTES',
    '/my-day':'OPERATIONS TODAY',
    '/staff-admin':"TODAY'S OPERATIONS",
}.items():
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=12) as r:
        body=r.read().decode('utf-8')
        if r.status != 200: raise SystemExit(f'{path}: HTTP {r.status}')
        if marker not in body: raise SystemExit(f'{path}: marker missing: {marker}')
        print(f'{path}: HTTP 200 / marker OK')
PY

docker exec citymanager-dashboard sh -lc "grep -q '/operations-runs/{{ run.run_id }}/note' /app/templates/operations_my_day_insert.html && grep -q '/operations-runs/{{ run.run_id }}/note' /app/templates/operations_admin_insert.html"

log "Confirming public Employee Operations was not disturbed"
PUBLIC_AFTER="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC_AFTER"
grep -q '"version":3' <<<"$PUBLIC_AFTER" || fail "Public Operations portal is not version 3 after deployment."
grep -q '"secure_cookies":true' <<<"$PUBLIC_AFTER" || fail "Public secure cookies are not active."

log "Confirming staff port remains localhost-only"
PORT_LINE="$(ss -lntp | grep -E ':8091\b' || true)"
printf '%s\n' "$PORT_LINE"
[[ -n "$PORT_LINE" ]] || fail "Nothing is listening on 8091."
if grep -Eq '0\.0\.0\.0:8091|\*:8091|\[::\]:8091' <<<"$PORT_LINE"; then fail "Port 8091 became publicly bound."; fi
grep -q '127\.0\.0\.1:8091' <<<"$PORT_LINE" || fail "Port 8091 is not localhost-only."

cd "$REPO"
log "Running full City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then deploy/cmos-health; else bash deploy/cmos-health; fi

log "Final repository state"
git status --short
log "PHASE 3 OCCURRENCE CONTROLS PASSED"
log "Routine setup: http://100.94.203.47:8090/operations-routines"
log "My Day: http://100.94.203.47:8090/my-day"
log "Operations Board: http://100.94.203.47:8090/staff-admin"
log "Deployment commit: ${DEPLOYED_HEAD}"
log "Database backup: ${BACKUP_DIR}/citymanager-pre-occurrence-controls.dump"
