#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase3/operations-engine"
BACKUP_ROOT="/var/backups/city-manager-os/phase3-operations"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "PHASE 3 OPERATIONS ENGINE FAILED with exit code ${rc}. Review this log before retrying or restarting anything."; exit $rc' ERR

for cmd in git docker curl ss; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done

cd "$REPO"
log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then git status --short; fail "Repository is not clean. No changes were made."; fi
ORIGINAL_BRANCH="$(git branch --show-current)"
ORIGINAL_HEAD="$(git rev-parse HEAD)"
log "Starting from ${ORIGINAL_BRANCH:-detached} @ ${ORIGINAL_HEAD}"

log "Confirming current private and public applications are healthy"
docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())"
PUBLIC_BEFORE="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC_BEFORE"
grep -q '"version":3' <<<"$PUBLIC_BEFORE" || fail "Public Operations portal is not version 3 before deployment."

log "Fetching Phase 3 branch"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
DEPLOYED_HEAD="$(git rev-parse HEAD)"
log "Deploying ${DEPLOYED_HEAD}"

for file in deploy/postgis/init/012_operations_engine.sql dashboard/operations_engine.py dashboard/operations_routines_app.py dashboard/phase3_app.py dashboard/templates/operations_admin_insert.html dashboard/templates/operations_my_day_insert.html dashboard/templates/operations_routines.html dashboard/static/operations_v3.css; do [[ -s "$file" ]] || fail "Missing Phase 3 file: $file"; done

STAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"
mkdir -p "$BACKUP_DIR"
printf '%s\n' "$ORIGINAL_BRANCH" > "$BACKUP_DIR/original_branch.txt"
printf '%s\n' "$ORIGINAL_HEAD" > "$BACKUP_DIR/original_head.txt"
printf '%s\n' "$DEPLOYED_HEAD" > "$BACKUP_DIR/deployment_head.txt"

log "Backing up PostgreSQL before schema migration"
docker exec citymanager-postgis sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_DIR/citymanager-pre-phase3.dump"
[[ -s "$BACKUP_DIR/citymanager-pre-phase3.dump" ]] || fail "Database backup is empty."
log "Database backup created at $BACKUP_DIR/citymanager-pre-phase3.dump"

cd "$REPO/dashboard"
log "Validating compose configuration"
docker compose config >/tmp/cmos-phase3-compose.txt
log "Building shared Phase 3 application image"
docker compose build citymanager-dashboard

log "Compiling Phase 3 Python modules in built image"
docker run --rm --entrypoint python dashboard-citymanager-dashboard:latest -m py_compile operations_engine.py operations_routines_app.py phase3_app.py employee_app.py employee_public_app.py staff_admin_app.py schedule_app.py

log "Parsing all Jinja templates in built image"
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
log "Applying idempotent Phase 3 database migration"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < deploy/postgis/init/012_operations_engine.sql

log "Verifying Phase 3 tables, columns and completion trigger"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL' >/tmp/cmos-phase3-db-check.txt
SELECT to_regclass('public.operations_routines');
SELECT to_regclass('public.operations_routine_runs');
SELECT count(*) FROM information_schema.columns WHERE table_name='issues' AND column_name IN ('operations_routine_id','operations_run_id','verification_required','verification_pending','verified_at','verified_by','verification_note');
SELECT count(*) FROM pg_trigger WHERE tgname='trg_cmos_hold_completion_for_verification' AND NOT tgisinternal;
SQL
cat /tmp/cmos-phase3-db-check.txt
grep -q '^operations_routines$' /tmp/cmos-phase3-db-check.txt || fail "operations_routines table missing."
grep -q '^operations_routine_runs$' /tmp/cmos-phase3-db-check.txt || fail "operations_routine_runs table missing."
grep -q '^7$' /tmp/cmos-phase3-db-check.txt || fail "Phase 3 issue columns incomplete."
grep -q '^1$' /tmp/cmos-phase3-db-check.txt || fail "Verification trigger missing."

cd "$REPO/dashboard"
log "Running read-only Operations Engine dry run"
docker run --rm --network citymanager --env-file .env -e DB_HOST=citymanager-postgis -e DB_PORT=5432 -e DB_NAME=citymanager -e DB_USER=citymanager_app -e TZ=America/New_York -e PGTZ=America/New_York dashboard-citymanager-dashboard:latest python operations_engine.py --dry-run

log "Recreating both web apps and starting Operations Engine"
docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-staff citymanager-ops-engine

log "Waiting for private dashboard"
DASH_OK=0
for _ in $(seq 1 30); do if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-phase3-dashboard-health.txt 2>/dev/null; then DASH_OK=1; break; fi; sleep 2; done
[[ "$DASH_OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-phase3-dashboard-health.txt

log "Waiting for public Operations portal version 3"
STAFF_OK=0
for _ in $(seq 1 30); do STAFF_HEALTH="$(curl -fsS --max-time 8 https://ops.nhnj.us/health 2>/dev/null || true)"; if grep -q '"version":3' <<<"$STAFF_HEALTH"; then STAFF_OK=1; printf '%s\n' "$STAFF_HEALTH"; break; fi; sleep 2; done
[[ "$STAFF_OK" -eq 1 ]] || fail "Public Operations portal did not become healthy."
grep -q '"secure_cookies":true' <<<"$STAFF_HEALTH" || fail "Secure cookies are not active."

log "Waiting for Operations Engine health"
ENGINE_OK=0
for _ in $(seq 1 30); do ENGINE_STATUS="$(docker exec citymanager-postgis sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT status FROM source_health WHERE source_id='\''OPERATIONS_ENGINE'\'';"' 2>/dev/null || true)"; if [[ "$ENGINE_STATUS" == "OK" ]]; then ENGINE_OK=1; break; fi; sleep 2; done
[[ "$ENGINE_OK" -eq 1 ]] || { docker logs --tail 120 citymanager-ops-engine || true; fail "Operations Engine did not report OK."; }
echo "OPERATIONS_ENGINE=$ENGINE_STATUS"

log "Checking Operations Engine container state"
docker ps --filter name=citymanager-ops-engine --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker logs --tail 20 citymanager-ops-engine

log "Testing Phase 3 private routes"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
checks={'/':'City Manager OS','/my-day':'OPERATIONS TODAY','/staff-admin':"TODAY'S OPERATIONS",'/operations-routines':'Daily &amp; Recurring Operations'}
for path, marker in checks.items():
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=12) as r:
        body=r.read().decode('utf-8')
        if r.status != 200: raise SystemExit(f'{path}: HTTP {r.status}')
        if marker not in body: raise SystemExit(f'{path}: Phase 3 marker missing: {marker}')
        print(f'{path}: HTTP 200 / marker OK')
PY

log "Checking public employee pending-verification UI is deployed"
curl -fsS https://ops.nhnj.us/static/staff.css >/dev/null
docker exec citymanager-staff sh -lc "grep -q 'PENDING_VERIFICATION' /app/templates/staff_ticket.html && grep -q 'Submit for Completion' /app/templates/staff_ticket.html"

log "Confirming staff port remains localhost-only"
PORT_LINE="$(ss -lntp | grep -E ':8091\b' || true)"
printf '%s\n' "$PORT_LINE"
[[ -n "$PORT_LINE" ]] || fail "Nothing is listening on 8091."
if grep -Eq '0\.0\.0\.0:8091|\*:8091|\[::\]:8091' <<<"$PORT_LINE"; then fail "Port 8091 became publicly bound."; fi
grep -q '127\.0\.0\.1:8091' <<<"$PORT_LINE" || fail "Port 8091 is not localhost-only."

log "Confirming Eastern Time on all Phase 3 app containers"
for container in citymanager-dashboard citymanager-staff citymanager-ops-engine; do ENV_TEXT="$(docker exec "$container" sh -c 'printf "TZ=%s\nPGTZ=%s\n" "$TZ" "$PGTZ"')"; printf '%s\n' "$ENV_TEXT"; grep -q 'TZ=America/New_York' <<<"$ENV_TEXT" || fail "$container TZ is not America/New_York."; grep -q 'PGTZ=America/New_York' <<<"$ENV_TEXT" || fail "$container PGTZ is not America/New_York."; done

cd "$REPO"
log "Running full City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then deploy/cmos-health; else bash deploy/cmos-health; fi
log "Final repository state"
git status --short
log "PHASE 3 OPERATIONS ENGINE PASSED"
log "Operations setup: http://100.94.203.47:8090/operations-routines"
log "Operations Board: http://100.94.203.47:8090/staff-admin"
log "My Day: http://100.94.203.47:8090/my-day"
log "Employee portal: https://ops.nhnj.us/staff"
log "Deployment commit: ${DEPLOYED_HEAD}"
log "Database backup: ${BACKUP_DIR}/citymanager-pre-phase3.dump"
