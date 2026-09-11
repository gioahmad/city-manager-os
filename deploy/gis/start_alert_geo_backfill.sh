#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
LIMIT="${ALERT_GEO_BACKFILL_LIMIT:-10000}"
SINCE_DAYS="${ALERT_GEO_BACKFILL_DAYS:-3650}"
RUN_ID="$(date '+%Y%m%d%H%M%S')"
UNIT="cmos-alert-geo-backfill-${RUN_ID}"
LOG="/var/log/cmos-alert-geo-backfill.log"
UNIT_RECORD="/opt/citymanager-data/gis/manifests/CURRENT_ALERT_GEO_BACKFILL_UNIT"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "ALERT GEO BACKFILL START FAILED with exit code ${rc}."; exit $rc' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"
[[ "$LIMIT" =~ ^[0-9]+$ && "$LIMIT" -ge 1 && "$LIMIT" -le 10000 ]] || fail "limit must be 1..10000"
[[ "$SINCE_DAYS" =~ ^[0-9]+$ && "$SINCE_DAYS" -ge 1 && "$SINCE_DAYS" -le 3650 ]] || fail "days must be 1..3650"
[[ "$(docker inspect citymanager-integration-engine --format '{{.State.Running}}')" == true ]] || fail "integration engine is not running"

if [[ -f "$UNIT_RECORD" ]]; then
  prior="$(cat "$UNIT_RECORD")"
  [[ "$(systemctl is-active "$prior" 2>/dev/null || true)" != activating ]] || fail "backfill already running as ${prior}"
fi

install -d -m 0750 "$(dirname "$UNIT_RECORD")"
printf '%s\n' "$UNIT" > "$UNIT_RECORD"
log "Starting bounded local alert geography backfill unit=${UNIT} limit=${LIMIT} days=${SINCE_DAYS}" | tee -a "$LOG"

systemd-run --no-block \
  --unit="$UNIT" \
  --description="City Manager OS Alert Geography Backfill" \
  --property=Type=oneshot \
  --property="StandardOutput=append:${LOG}" \
  --property="StandardError=append:${LOG}" \
  /usr/bin/docker exec citymanager-integration-engine \
  python /app/geo_resolver.py backfill --limit "$LIMIT" --since-days "$SINCE_DAYS"

sleep 2
systemctl show "$UNIT" -p ActiveState -p SubState -p Result -p ExecMainPID -p ExecMainStatus
log "Backfill continues under systemd and may be monitored from any SSH session."
