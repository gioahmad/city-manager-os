#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
SERVICE="/etc/systemd/system/city-manager-os-full-backup.service"
TIMER="/etc/systemd/system/city-manager-os-full-backup.timer"

[[ "$(id -u)" == "0" ]] || { echo "ERROR: run as root"; exit 1; }
command -v systemctl >/dev/null || { echo "ERROR: systemd is required"; exit 1; }

cat > "$SERVICE" <<EOF
[Unit]
Description=City Manager OS validated full PostgreSQL backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=/bin/bash $REPO/deploy/postgis/backup.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

cat > "$TIMER" <<'EOF'
[Unit]
Description=Nightly City Manager OS full backup

[Timer]
OnCalendar=*-*-* 03:20:00 America/New_York
RandomizedDelaySec=20m
Persistent=true
Unit=city-manager-os-full-backup.service

[Install]
WantedBy=timers.target
EOF

chmod 644 "$SERVICE" "$TIMER"
systemctl daemon-reload
systemctl enable --now city-manager-os-full-backup.timer
systemctl list-timers city-manager-os-full-backup.timer --no-pager

echo "FULL BACKUP TIMER: PASS — nightly around 03:20 local time with randomized delay."
