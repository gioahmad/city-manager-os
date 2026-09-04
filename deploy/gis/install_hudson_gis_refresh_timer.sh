#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
SERVICE="/etc/systemd/system/cmos-hudson-gis-refresh.service"
TIMER="/etc/systemd/system/cmos-hudson-gis-refresh.timer"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "GIS REFRESH TIMER INSTALL FAILED with exit code ${rc}."; exit $rc' ERR

for cmd in git systemctl bash docker; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

cd "$REPO"
log "Synchronizing repository with origin/main"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean."
fi
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ ${HEAD_SHA}"

log "Syntax-checking GIS refresh scripts"
bash -n deploy/gis/refresh_hudson_gis.sh
bash -n deploy/gis/notify_gis_refresh.sh
bash -n deploy/gis/import_gis.sh
bash -n deploy/gis/promote_hudson_gis.sh

log "Running validation-only refresh preflight"
GIS_REFRESH_MODE=validate bash deploy/gis/refresh_hudson_gis.sh

log "Installing systemd service"
cat > "$SERVICE" <<'UNIT'
[Unit]
Description=City Manager OS Hudson County GIS Refresh
Wants=docker.service network-online.target
After=docker.service network-online.target
ConditionPathExists=/opt/city-manager-os/deploy/gis/refresh_hudson_gis.sh

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/city-manager-os
ExecStart=/usr/bin/bash /opt/city-manager-os/deploy/gis/refresh_hudson_gis.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=12h
StandardOutput=append:/var/log/cmos-hudson-gis-refresh.log
StandardError=append:/var/log/cmos-hudson-gis-refresh.log

[Install]
WantedBy=multi-user.target
UNIT

log "Installing monthly systemd timer"
cat > "$TIMER" <<'UNIT'
[Unit]
Description=Monthly City Manager OS Hudson GIS Refresh

[Timer]
# First Sunday of each month at 03:15 Eastern, with up to 15 minutes of jitter.
OnCalendar=Sun *-*-01..07 03:15:00 America/New_York
RandomizedDelaySec=15m
Persistent=true
Unit=cmos-hudson-gis-refresh.service

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now cmos-hudson-gis-refresh.timer

log "Verifying installed timer and service"
systemctl is-enabled cmos-hudson-gis-refresh.timer
systemctl is-active cmos-hudson-gis-refresh.timer
systemctl cat cmos-hudson-gis-refresh.timer
systemctl list-timers cmos-hudson-gis-refresh.timer --all --no-pager

log "Verifying current production GIS remains healthy"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'gis_parcels' AS table_name,count(*) AS rows,
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_geom
FROM gis_parcels
UNION ALL
SELECT 'gis_addresses',count(*),
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom))
FROM gis_addresses;
SELECT dataset_id,status,row_count,imported_at
FROM gis_dataset_versions
WHERE dataset_id IN ('NJOGIS_HUDSON_PARCELS','NJOGIS_HUDSON_ADDRESSES')
ORDER BY dataset_id;
SQL

log "HUDSON GIS MONTHLY REFRESH TIMER PASSED"
log "Schedule: first Sunday of each month at 03:15 America/New_York, randomized up to 15 minutes"
log "Service log: /var/log/cmos-hudson-gis-refresh.log"
log "Repository commit: ${HEAD_SHA}"
