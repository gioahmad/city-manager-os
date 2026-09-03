#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/supervisor-operations-board"
LOG_PREFIX="PHASE 2 SUPERVISOR BOARD"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

on_error() {
  local rc=$?
  log "DEPLOYMENT FAILED with exit code ${rc}."
  log "Review this log before retrying."
  exit "$rc"
}
trap on_error ERR

command -v git >/dev/null || fail "git is required"
command -v docker >/dev/null || fail "docker is required"
command -v curl >/dev/null || fail "curl is required"

cd "$REPO"

log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean. No changes were made."
fi

ORIGINAL_BRANCH="$(git branch --show-current)"
ORIGINAL_HEAD="$(git rev-parse HEAD)"
log "Starting from ${ORIGINAL_BRANCH:-detached} @ ${ORIGINAL_HEAD}"

log "Confirming public Operations portal is healthy before dashboard deployment"
PUBLIC_BEFORE="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC_BEFORE"
grep -q '"version":3' <<<"$PUBLIC_BEFORE" || fail "Public Operations portal is not version 3 before deployment."

log "Fetching supervisor board branch"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
DEPLOYED_HEAD="$(git rev-parse HEAD)"
log "Deploying ${DEPLOYED_HEAD}"

log "Compiling supervisor backend"
python3 -m py_compile dashboard/staff_admin_app.py

log "Parsing supervisor template"
python3 - <<'PY'
from pathlib import Path
from jinja2 import Environment
Environment().parse(Path('dashboard/templates/staff_admin.html').read_text())
print('Template parse: OK')
PY

cd "$REPO/dashboard"
log "Validating compose configuration"
docker compose config >/tmp/cmos-phase2-supervisor-compose.txt

log "Building dashboard image"
docker compose build citymanager-dashboard

log "Recreating only the private City Manager OS dashboard"
docker compose up -d --no-deps --force-recreate citymanager-dashboard

log "Waiting for private dashboard health"
DASH_OK=0
for _ in $(seq 1 20); do
  if docker exec citymanager-dashboard python -c "import json,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-dashboard-health.txt 2>/dev/null; then
    DASH_OK=1
    break
  fi
  sleep 2
done
[[ "$DASH_OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-dashboard-health.txt

log "Testing Operations Board views against live database without modifying data"
docker exec citymanager-dashboard python - <<'PY'
import urllib.request

base = 'http://127.0.0.1:8000/staff-admin'
views = [
    'active',
    'unassigned',
    'assigned',
    'in_progress',
    'needs_help',
    'completed',
]
for view in views:
    url = f'{base}?view={view}'
    with urllib.request.urlopen(url, timeout=10) as r:
        body = r.read().decode('utf-8')
        if r.status != 200:
            raise SystemExit(f'{view}: HTTP {r.status}')
        if 'Operations Board' not in body:
            raise SystemExit(f'{view}: board marker missing')
        print(f'{view}: HTTP 200')
PY

log "Checking that supervisor controls are rendered"
docker exec citymanager-dashboard python - <<'PY'
import urllib.request
body = urllib.request.urlopen(
    'http://127.0.0.1:8000/staff-admin?view=active',
    timeout=10,
).read().decode('utf-8')
required = [
    'TEAM WORKLOAD',
    'Assign / Reassign',
    'Supervisor Instruction',
    'OPERATIONS SETUP',
]
missing = [x for x in required if x not in body]
if missing:
    raise SystemExit('Missing board controls: ' + ', '.join(missing))
print('Board controls: OK')
PY

log "Confirming employee Operations portal stayed healthy"
PUBLIC_AFTER="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC_AFTER"
grep -q '"version":3' <<<"$PUBLIC_AFTER" || fail "Public Operations portal changed or failed."
grep -q '"secure_cookies":true' <<<"$PUBLIC_AFTER" || fail "Secure-cookie mode is no longer active."

log "Confirming staff port remains localhost-only"
PORT_LINE="$(ss -lntp | grep -E ':8091\b' || true)"
printf '%s\n' "$PORT_LINE"
[[ -n "$PORT_LINE" ]] || fail "Nothing is listening on 8091."
if grep -Eq '0\.0\.0\.0:8091|\*:8091|\[::\]:8091' <<<"$PORT_LINE"; then
  fail "Port 8091 became publicly bound."
fi

cd "$REPO"
log "Running full City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then
  deploy/cmos-health
else
  bash deploy/cmos-health
fi

log "Final repository state"
git status --short

log "${LOG_PREFIX} PASSED"
log "Private board: http://100.94.203.47:8090/staff-admin"
log "Deployed commit: ${DEPLOYED_HEAD}"
log "Rollback if needed: git switch main && cd dashboard && docker compose build citymanager-dashboard && docker compose up -d --no-deps --force-recreate citymanager-dashboard"
