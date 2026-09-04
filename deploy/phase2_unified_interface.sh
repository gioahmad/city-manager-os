#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/unified-interface"
PUBLIC_ORIGIN="https://ops.nhnj.us"
LOG_PREFIX="PHASE 2 UNIFIED INTERFACE"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }
on_error() { local rc=$?; log "DEPLOYMENT FAILED with exit code ${rc}."; log "Review this log before retrying."; exit "$rc"; }
trap on_error ERR

for cmd in git docker curl ss; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done

cd "$REPO"
log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then git status --short; fail "Repository is not clean. No changes were made."; fi

ORIGINAL_BRANCH="$(git branch --show-current)"
ORIGINAL_HEAD="$(git rev-parse HEAD)"
log "Starting from ${ORIGINAL_BRANCH:-detached} @ ${ORIGINAL_HEAD}"

log "Confirming public Operations portal is healthy before deployment"
PUBLIC_BEFORE="$(curl -fsS --max-time 10 "$PUBLIC_ORIGIN/health")"
printf '%s\n' "$PUBLIC_BEFORE"
grep -q '"version":3' <<<"$PUBLIC_BEFORE" || fail "Public Operations portal is not version 3 before deployment."

log "Fetching unified interface branch"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
DEPLOYED_HEAD="$(git rev-parse HEAD)"
log "Deploying ${DEPLOYED_HEAD}"

for file in dashboard/static/style.css dashboard/static/ops.css dashboard/static/staff.css dashboard/templates/nav.html dashboard/templates/staff_admin.html dashboard/templates/staff_login.html dashboard/templates/staff_home.html dashboard/templates/staff_report.html dashboard/templates/staff_work.html dashboard/templates/staff_supervisor.html dashboard/templates/staff_ticket.html; do [[ -s "$file" ]] || fail "Missing interface file: $file"; done

grep -q -- '--brand:#102a43' dashboard/static/style.css || fail "Private design-system marker missing."
grep -q 'mobile-nav' dashboard/templates/nav.html || fail "Responsive navigation marker missing."
grep -q 'Operations Board' dashboard/templates/staff_admin.html || fail "Operations Board template marker missing."
grep -q -- '--staff-brand:#102a43' dashboard/static/staff.css || fail "Employee design-system marker missing."

cd "$REPO/dashboard"
log "Validating compose configuration"
docker compose config >/tmp/cmos-phase2-ui-compose.txt

log "Building shared City Manager OS image"
docker compose build citymanager-dashboard

log "Parsing all Jinja templates in the built image"
docker run --rm -i --entrypoint python dashboard-citymanager-dashboard:latest - <<'PY'
from pathlib import Path
from jinja2 import Environment

env = Environment()
files = sorted(Path('/app/templates').glob('*.html'))
if not files:
    raise SystemExit('No templates found')
for path in files:
    env.parse(path.read_text())
print(f'Template parse: OK ({len(files)} templates)')
PY

log "Recreating private dashboard and public Operations app"
docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-staff

log "Waiting for private dashboard health"
DASH_OK=0
for _ in $(seq 1 25); do
  if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-ui-dashboard-health.txt 2>/dev/null; then DASH_OK=1; break; fi
  sleep 2
done
[[ "$DASH_OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-ui-dashboard-health.txt

log "Waiting for public Operations health"
STAFF_OK=0
for _ in $(seq 1 25); do
  STAFF_HEALTH="$(curl -fsS --max-time 8 "$PUBLIC_ORIGIN/health" 2>/dev/null || true)"
  if grep -q '"version":3' <<<"$STAFF_HEALTH"; then STAFF_OK=1; printf '%s\n' "$STAFF_HEALTH"; break; fi
  sleep 2
done
[[ "$STAFF_OK" -eq 1 ]] || fail "Public Operations portal did not become healthy."
grep -q '"secure_cookies":true' <<<"$STAFF_HEALTH" || fail "Secure cookies are not active."

log "Verifying Eastern Time on BOTH application containers"
for container in citymanager-dashboard citymanager-staff; do
  docker exec -i "$container" python - <<'PY'
import datetime
import os
import psycopg
print("TZ=" + str(os.getenv("TZ")))
print("PGTZ=" + str(os.getenv("PGTZ")))
print("PY_NOW=" + datetime.datetime.now().astimezone().isoformat())
conn = psycopg.connect(host=os.getenv("DB_HOST","citymanager-postgis"),port=int(os.getenv("DB_PORT","5432")),dbname=os.getenv("DB_NAME","citymanager"),user=os.getenv("DB_USER","citymanager_app"),password=os.environ["DB_PASSWORD"])
with conn.cursor() as cur:
    cur.execute("SHOW TIME ZONE")
    print("DB_TIMEZONE=" + cur.fetchone()[0])
    cur.execute("SELECT now()")
    print("DB_NOW=" + cur.fetchone()[0].isoformat())
conn.close()
PY
done

for container in citymanager-dashboard citymanager-staff; do
  ENV_TEXT="$(docker exec "$container" sh -c 'printf "TZ=%s\nPGTZ=%s\n" "$TZ" "$PGTZ"')"
  grep -q 'TZ=America/New_York' <<<"$ENV_TEXT" || fail "$container TZ is not America/New_York."
  grep -q 'PGTZ=America/New_York' <<<"$ENV_TEXT" || fail "$container PGTZ is not America/New_York."
done

log "Testing all private City Manager OS pages"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
paths=['/','/my-day','/schedule','/alerts','/modules','/issues','/staff-admin','/rules','/watchlist','/subscribers','/routing','/source-health','/deliveries']
for path in paths:
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=12) as r:
        body=r.read().decode('utf-8')
        if r.status != 200:
            raise SystemExit(f'{path}: HTTP {r.status}')
        if 'mobile-nav' not in body:
            raise SystemExit(f'{path}: responsive navigation missing')
        print(f'{path}: HTTP 200')
PY

log "Checking public employee interface and stylesheet"
LOGIN_HTML="$(mktemp)"
LOGIN_STATUS="$(curl -fsS -o "$LOGIN_HTML" -w '%{http_code}' "$PUBLIC_ORIGIN/staff")"
[[ "$LOGIN_STATUS" == "200" ]] || fail "Public employee login returned HTTP ${LOGIN_STATUS}."
grep -q 'WEEHAWKEN OPERATIONS' "$LOGIN_HTML" || fail "Employee brand marker missing."
grep -q 'Employee Login' "$LOGIN_HTML" || fail "Employee login marker missing."
rm -f "$LOGIN_HTML"
STAFF_CSS="$(mktemp)"
curl -fsS "$PUBLIC_ORIGIN/static/staff.css" > "$STAFF_CSS"
grep -q -- '--staff-brand:#102a43' "$STAFF_CSS" || fail "Public staff stylesheet is not the unified design."
rm -f "$STAFF_CSS"

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
log "${LOG_PREFIX} PASSED"
log "Private UI: http://100.94.203.47:8090"
log "Operations Board: http://100.94.203.47:8090/staff-admin"
log "Employee UI: https://ops.nhnj.us/staff"
log "Deployed commit: ${DEPLOYED_HEAD}"
log "Rollback: git switch main && cd dashboard && docker compose build citymanager-dashboard && docker compose up -d --no-deps --force-recreate citymanager-dashboard citymanager-staff"
