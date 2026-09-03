#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/unified-interface"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "LIGHT NAV FIX FAILED with exit code ${rc}. Review the log before retrying."; exit $rc' ERR

cd "$REPO"

log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean."
fi

log "Fetching interface branch"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
DEPLOYED_HEAD="$(git rev-parse HEAD)"
log "Deploying ${DEPLOYED_HEAD}"

grep -q 'command_nav.css?v=20260903-2' dashboard/templates/nav.html || fail "Fresh compact nav stylesheet marker missing."
grep -q 'background:rgba(255,255,255,.97)' dashboard/static/command_nav.css || fail "Light navigation background marker missing."
grep -q 'min-height:40px' dashboard/static/command_nav.css || fail "Compact navigation height marker missing."
grep -q 'background:#2b7db8' dashboard/static/command_nav.css || fail "Existing blue active marker missing."

cd "$REPO/dashboard"
log "Building shared dashboard image"
docker compose build citymanager-dashboard

log "Recreating ONLY private City Manager OS dashboard"
docker compose up -d --no-deps --force-recreate citymanager-dashboard

log "Waiting for private dashboard"
OK=0
for _ in $(seq 1 20); do
  if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-light-nav-health.txt 2>/dev/null; then
    OK=1
    break
  fi
  sleep 2
done
[[ "$OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-light-nav-health.txt

log "Verifying navigation on representative pages"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
for path in ['/', '/issues', '/staff-admin', '/rules']:
    body=urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=10).read().decode('utf-8')
    required=['desktop-nav','Overview','Command Center','Operations Board','Rules Center','mobile-nav','command_nav.css?v=20260903-2']
    missing=[x for x in required if x not in body]
    if missing:
        raise SystemExit(f'{path}: missing {missing}')
    print(f'{path}: nav OK')
PY

log "Confirming public Operations portal remains healthy"
PUBLIC="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC"
grep -q '"version":3' <<<"$PUBLIC" || fail "Public Operations portal health changed."
grep -q '"secure_cookies":true' <<<"$PUBLIC" || fail "Secure cookies no longer active."

cd "$REPO"
log "Running City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then deploy/cmos-health; else bash deploy/cmos-health; fi

log "LIGHT NAV FIX PASSED"
log "Private UI: http://100.94.203.47:8090"
log "Deployed commit: ${DEPLOYED_HEAD}"
