#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
PREVIOUS="${1:-}"
TARGET="${2:-$(git -C "$REPO" rev-parse HEAD)}"
MAX_AGE_HOURS="${CMOS_DEPLOY_BACKUP_MAX_AGE_HOURS:-12}"

cd "$REPO"

force=false
reason="routine application release"

if [[ "${CMOS_FORCE_FULL_BACKUP:-false}" == "true" ]]; then
  force=true
  reason="CMOS_FORCE_FULL_BACKUP requested"
elif [[ -z "$PREVIOUS" ]] || ! git cat-file -e "$PREVIOUS^{commit}" 2>/dev/null; then
  force=true
  reason="no prior release marker available"
elif git diff --name-only "$PREVIOUS" "$TARGET" --     deploy/postgis/init     deploy/gis     schemas     dashboard/gis_import.py     dashboard/geo_resolver.py     dashboard/spatial_reference_app.py     dashboard/spatial_watch_app.py |
    grep -q .; then
  force=true
  reason="database/GIS-sensitive files changed"
fi

if [[ "$force" == "true" ]]; then
  echo "BACKUP GATE: fresh full logical backup required — $reason"
  bash deploy/postgis/backup.sh
  exit 0
fi

echo "BACKUP GATE: routine release — checking for validated backup <= ${MAX_AGE_HOURS}h"
if BACKUP_MAX_AGE_HOURS="$MAX_AGE_HOURS" bash deploy/postgis/verify-backup.sh; then
  echo "BACKUP GATE: PASS — reusing recent validated recovery point; no new multi-GB dump required"
else
  echo "BACKUP GATE: recent recovery point unavailable — creating fresh full logical backup"
  bash deploy/postgis/backup.sh
fi
