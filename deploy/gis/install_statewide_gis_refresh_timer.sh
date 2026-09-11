#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
SERVICE="/etc/systemd/system/cmos-nj-statewide-gis-refresh.service"
TIMER="/etc/systemd/system/cmos-nj-statewide-gis-refresh.timer"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "NJ STATEWIDE GIS TIMER INSTALL FAILED with exit code ${rc}."; exit $rc' ERR

for cmd in git systemctl bash docker python3; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"

log "Validating statewide lifecycle code"
bash -n deploy/gis/refresh_statewide_gis.sh
bash -n deploy/gis/import_statewide_gis.sh
bash -n deploy/gis/notify_gis_refresh.sh
python3 -m py_compile deploy/gis/statewide_bulk_refresh.py deploy/gis/test_statewide_bulk_refresh.py
PYTHONPATH=deploy/gis python3 deploy/gis/test_statewide_bulk_refresh.py

log "Installing refresh run ledger"
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < deploy/postgis/init/029_statewide_gis_refresh_control.sql

log "Installing monthly statewide service and timer"
install -m 0644 /dev/stdin "$SERVICE" <<'UNIT'
[Unit]
Description=City Manager OS NJ Statewide GIS Refresh
Wants=docker.service network-online.target
After=docker.service network-online.target
ConditionPathExists=/opt/city-manager-os/deploy/gis/refresh_statewide_gis.sh

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/city-manager-os
ExecStart=/usr/bin/bash /opt/city-manager-os/deploy/gis/refresh_statewide_gis.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=24h
StandardOutput=append:/var/log/cmos-nj-statewide-gis-refresh.log
StandardError=append:/var/log/cmos-nj-statewide-gis-refresh.log
UNIT

install -m 0644 /dev/stdin "$TIMER" <<'UNIT'
[Unit]
Description=Monthly City Manager OS NJ Statewide GIS Refresh

[Timer]
OnCalendar=Sun *-*-01..07 03:15:00 America/New_York
RandomizedDelaySec=15m
Persistent=true
Unit=cmos-nj-statewide-gis-refresh.service

[Install]
WantedBy=timers.target
UNIT

systemctl disable --now cmos-hudson-gis-refresh.timer 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now cmos-nj-statewide-gis-refresh.timer
systemctl is-enabled cmos-nj-statewide-gis-refresh.timer >/dev/null
systemctl is-active cmos-nj-statewide-gis-refresh.timer >/dev/null

log "NJ STATEWIDE GIS MONTHLY REFRESH TIMER PASSED"
systemctl list-timers cmos-nj-statewide-gis-refresh.timer --all --no-pager
log "The installer did not start a refresh or change production GIS tables."
