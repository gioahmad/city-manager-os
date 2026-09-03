#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BRANCH="phase2/staff-access-hardening"
PUBLIC_HOST="ops.nhnj.us"
PUBLIC_ORIGIN="https://${PUBLIC_HOST}"
EXPECTED_PUBLIC_IP="${EXPECTED_PUBLIC_IP:-69.164.245.248}"
CADDYFILE="/opt/docker/caddy/Caddyfile"
BACKUP_ROOT="/var/backups/city-manager-os/phase2-ops"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

on_error() {
  local rc=$?
  log "CUTOVER FAILED with exit code ${rc}."
  log "Backups are in ${BACKUP_DIR:-not-created}."
  log "Do not restart the deployment from the beginning until the log is reviewed."
  exit "$rc"
}
trap on_error ERR

command -v git >/dev/null || fail "git is required"
command -v docker >/dev/null || fail "docker is required"
command -v curl >/dev/null || fail "curl is required"
command -v getent >/dev/null || fail "getent is required"
command -v python3 >/dev/null || fail "python3 is required"
command -v ss >/dev/null || fail "ss is required"

cd "$REPO"

log "Checking repository state"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean. No changes were made."
fi

CURRENT_HEAD="$(git rev-parse HEAD)"
CURRENT_BRANCH="$(git branch --show-current)"
log "Current branch: ${CURRENT_BRANCH:-detached} @ ${CURRENT_HEAD}"

log "Checking current employee operations health"
curl -fsS http://127.0.0.1:8091/health
printf '\n'

log "Checking public DNS for ${PUBLIC_HOST}"
DNS_IPS="$(getent ahostsv4 "$PUBLIC_HOST" 2>/dev/null | awk '{print $1}' | sort -u || true)"
if [[ -z "$DNS_IPS" ]]; then
  fail "${PUBLIC_HOST} does not resolve. Create an A record for ${PUBLIC_HOST} -> ${EXPECTED_PUBLIC_IP} first."
fi
printf '%s\n' "$DNS_IPS"
if ! grep -Fxq "$EXPECTED_PUBLIC_IP" <<<"$DNS_IPS"; then
  fail "${PUBLIC_HOST} does not resolve to expected VPS IP ${EXPECTED_PUBLIC_IP}."
fi

log "Fetching deployment branch"
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"

mkdir -p "$BACKUP_DIR"
cp -a "$CADDYFILE" "$BACKUP_DIR/Caddyfile.before"
cp -a dashboard/.env "$BACKUP_DIR/dashboard.env.before"
git rev-parse HEAD > "$BACKUP_DIR/deployed_commit.txt"
printf '%s\n' "$CURRENT_BRANCH" > "$BACKUP_DIR/original_branch.txt"
printf '%s\n' "$CURRENT_HEAD" > "$BACKUP_DIR/original_head.txt"

log "Backups created at $BACKUP_DIR"

log "Adding Caddy Operations site if needed"
if ! grep -Eq '^[[:space:]]*ops\.nhnj\.us[[:space:]]*\{' "$CADDYFILE"; then
  cat >> "$CADDYFILE" <<'CADDY'

ops.nhnj.us {
    reverse_proxy 127.0.0.1:8091
}
CADDY
fi

docker exec caddy caddy fmt --overwrite /etc/caddy/Caddyfile
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile

log "Checking HTTPS through Caddy before changing the staff container"
HTTPS_OK=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 10 "$PUBLIC_ORIGIN/health" >/tmp/cmos-ops-public-health-before.json 2>/dev/null; then
    HTTPS_OK=1
    break
  fi
  sleep 2
done
[[ "$HTTPS_OK" -eq 1 ]] || fail "HTTPS did not become healthy through Caddy. Inspect caddy logs before continuing."
cat /tmp/cmos-ops-public-health-before.json
printf '\n'

log "Enabling Phase 2 production Operations settings"
python3 - <<'PY'
from pathlib import Path

path = Path('/opt/city-manager-os/dashboard/.env')
updates = {
    'STAFF_SECURE_COOKIES': 'true',
    'STAFF_TRUST_PROXY': 'true',
    'STAFF_PUBLIC_ORIGIN': 'https://ops.nhnj.us',
    'STAFF_ALLOWED_HOSTS': 'ops.nhnj.us,localhost,127.0.0.1',
    'STAFF_BIND_IP': '127.0.0.1',
    'STAFF_LOGIN_MAX_FAILURES': '8',
    'STAFF_LOGIN_WINDOW_SECONDS': '900',
}

lines = path.read_text().splitlines()
seen = set()
out = []
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key = line.split('=', 1)[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
            continue
    out.append(line)

if out and out[-1] != '':
    out.append('')
for key, value in updates.items():
    if key not in seen:
        out.append(f'{key}={value}')

path.write_text('\n'.join(out).rstrip() + '\n')
PY

cd "$REPO/dashboard"
docker compose config >/tmp/cmos-phase2-compose-config.txt

log "Building Phase 2 dashboard image used by Operations"
docker compose build citymanager-dashboard

log "Recreating only the employee operations container"
docker compose up -d --no-deps --force-recreate citymanager-staff

log "Waiting for direct Operations health version 3"
LOCAL_OK=0
for _ in $(seq 1 20); do
  HEALTH="$(curl -fsS --max-time 5 http://127.0.0.1:8091/health 2>/dev/null || true)"
  if grep -q '"version":3' <<<"$HEALTH"; then
    LOCAL_OK=1
    printf '%s\n' "$HEALTH"
    break
  fi
  sleep 2
done
[[ "$LOCAL_OK" -eq 1 ]] || fail "Local Operations app did not become healthy as version 3."

log "Verifying port 8091 is no longer publicly bound"
PORT_LINE="$(ss -lntp | grep -E ':8091\b' || true)"
printf '%s\n' "$PORT_LINE"
[[ -n "$PORT_LINE" ]] || fail "Nothing is listening on 8091."
if grep -Eq '0\.0\.0\.0:8091|\*:8091|\[::\]:8091' <<<"$PORT_LINE"; then
  fail "Port 8091 is still publicly bound."
fi
if ! grep -q '127\.0\.0\.1:8091' <<<"$PORT_LINE"; then
  fail "Port 8091 is not bound to 127.0.0.1 as expected."
fi

log "Verifying public Phase 2 Operations health"
PUBLIC_HEALTH="$(curl -fsS --max-time 10 "$PUBLIC_ORIGIN/health")"
printf '%s\n' "$PUBLIC_HEALTH"
grep -q '"version":3' <<<"$PUBLIC_HEALTH" || fail "Public Operations health is not version 3."
grep -q '"secure_cookies":true' <<<"$PUBLIC_HEALTH" || fail "Secure-cookie mode is not active."

log "Checking canonical login page"
LOGIN_HTML="$(mktemp)"
LOGIN_STATUS="$(curl -fsS -o "$LOGIN_HTML" -w '%{http_code}' "$PUBLIC_ORIGIN/staff")"
[[ "$LOGIN_STATUS" == "200" ]] || fail "Canonical /staff login returned HTTP ${LOGIN_STATUS}."
if grep -q '/staff/[^" ]\+/login' "$LOGIN_HTML"; then
  fail "Login page appears to contain a tokenized login path."
fi
rm -f "$LOGIN_HTML"

log "Checking security headers"
HEADERS="$(curl -sS -D - -o /dev/null "$PUBLIC_ORIGIN/staff")"
grep -qi '^strict-transport-security:' <<<"$HEADERS" || fail "HSTS header missing."
grep -qi '^cache-control: no-store' <<<"$HEADERS" || fail "No-store header missing."
grep -qi '^x-frame-options: DENY' <<<"$HEADERS" || fail "Frame-denial header missing."

log "Checking container state"
docker ps --filter name=citymanager-staff --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'

cd "$REPO"
log "Running City Manager OS health check"
if [[ -x deploy/cmos-health ]]; then
  deploy/cmos-health
else
  bash deploy/cmos-health
fi

log "Final repository state"
git status --short

log "PHASE 2 OPERATIONS ACCESS CUTOVER PASSED"
log "Employee URL: ${PUBLIC_ORIGIN}/staff"
log "Deployment commit: $(git rev-parse HEAD)"
log "Backups: ${BACKUP_DIR}"
