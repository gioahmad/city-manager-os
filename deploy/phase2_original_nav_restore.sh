#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/unified-interface"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "ORIGINAL NAV RESTORE FAILED with exit code ${rc}. Review the log before retrying."; exit $rc' ERR

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

grep -q 'command_nav.css?v=20260903-3' dashboard/templates/nav.html || fail "Fresh nav stylesheet marker missing."
grep -q 'background:#0e1620' dashboard/static/command_nav.css || fail "Original dark nav background missing."
grep -q 'color:#91a3b5' dashboard/static/command_nav.css || fail "Original nav text color missing."
grep -q 'background:#121e2a' dashboard/static/command_nav.css || fail "Original hover/active background missing."
grep -q 'border-bottom-color:#57b8ff' dashboard/static/command_nav.css || fail "Original active underline missing."

cd "$REPO/dashboard"
log "Building shared dashboard image"
docker compose build citymanager-dashboard

log "Recreating ONLY private City Manager OS dashboard"
docker compose up -d --no-deps --force-recreate citymanager-dashboard

log "Waiting for private dashboard"
OK=0
for _ in $(seq 1 20); do
  if docker exec citymanager-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read().decode())" >/tmp/cmos-original-nav-health.txt 2>/dev/null; then
    OK=1
    break
  fi
  sleep 2
done
[[ "$OK" -eq 1 ]] || fail "Private dashboard did not become healthy."
cat /tmp/cmos-original-nav-health.txt

log "Verifying restored nav markup on representative pages"
docker exec -i citymanager-dashboard python - <<'PY'
import urllib.request
for path in ['/', '/issues', '/staff-admin', '/rules']:
    body=urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=10).read().decode('utf-8')
    required=['desktop-nav','Operations Board','Rules Center','mobile-nav','command_nav.css?v=20260903-3']
    missing=[x for x in required if x not in body]
    if missing:
        raise SystemExit(f'{path}: missing {missing}')
    print(f'{path}: restored nav OK')
PY

log "Confirming public Operations portal remains healthy"
PUBLIC="$(curl -fsS --max-time 10 https://ops.nhnj.us/health)"
printf '%s\n' "$PUBLIC"
grep -q '"version":3' <<<"$PUBLIC" || fail "Public Operations portal health changed."
grep -q '"secure_cookies":true' <<<"$PUBLIC" || fail "Secure cookies no longer active."

cd "$REPO"
log "Running City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then deploy/cmos-health; else bash deploy/cmos-health; fi

log "ORIGINAL NAV RESTORE PASSED"
log "Private UI: http://100.94.203.47:8090"
log "Deployed commit: ${DEPLOYED_HEAD}"
