#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/unified-interface"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "DENSITY HOTFIX FAILED with exit code ${rc}. Review the log before retrying."; exit $rc' ERR

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

[[ -s dashboard/static/density.css ]] || fail "density.css missing."
grep -q 'density.css?v=20260903-1' dashboard/templates/nav.html || fail "Density stylesheet link missing."
grep -q 'max-width:none' dashboard/static/density.css || fail "Fluid workspace marker missing."
grep -q 'repeat(3,minmax(0,1fr))' dashboard/static/density.css || fail "Wide Operations Board marker missing."
grep -q 'flex-direction:row!important' dashboard/templates/nav.html || fail "Horizontal nav hardening missing."

cd "$REPO/dashboard"
log "Building dashboard image"
docker compose build citymanager-dashboard

log "Recreating ONLY private City Manager OS dashboard"
docker compose up -d --no-deps --force-recreate citymanager-dashboard

log "Waiting for private dashboard"
OK=0
for _ in $(seq 1 20); do
  if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-density-health.txt 2>/dev/null; then
    OK=1
    break
  fi
  sleep 2
done
[[ "$OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-density-health.txt

log "Verifying representative pages load the density layer"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
for path in ['/', '/issues', '/staff-admin', '/rules']:
    body=urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=10).read().decode('utf-8')
    required=['desktop-nav','mobile-nav','density.css?v=20260903-1','Operations Board']
    if path != '/staff-admin':
        required.remove('Operations Board')
    missing=[x for x in required if x not in body]
    if missing:
        raise SystemExit(f'{path}: missing {missing}')
    print(f'{path}: layout markers OK')
PY

log "Verifying density stylesheet is served"
docker exec citymanager-dashboard python -c "import urllib.request; b=urllib.request.urlopen('http://127.0.0.1:8000/static/density.css',timeout=10).read().decode(); assert 'Desktop density layer' in b and 'repeat(3,minmax(0,1fr))' in b; print('density.css: OK')"

log "Confirming public Operations portal remains healthy"
PUBLIC="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC"
grep -q '"version":3' <<<"$PUBLIC" || fail "Public Operations portal health changed."
grep -q '"secure_cookies":true' <<<"$PUBLIC" || fail "Secure cookies no longer active."

cd "$REPO"
log "Running City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then deploy/cmos-health; else bash deploy/cmos-health; fi

log "DENSITY HOTFIX PASSED"
log "Private UI: http://100.94.203.47:8090"
log "Operations Board: http://100.94.203.47:8090/staff-admin"
log "Deployed commit: ${DEPLOYED_HEAD}"
