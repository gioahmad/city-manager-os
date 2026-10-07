#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
PREVIOUS="${1:-}"
TARGET="${2:-$(git -C "$REPO" rev-parse HEAD)}"
MAX_AGE_HOURS="${CMOS_DEPLOY_BACKUP_MAX_AGE_HOURS:-72}"

cd "$REPO"

# Explicit recovery from a failed application rollout is not a successful
# release marker. Reuse only the named, freshly revalidated archive, and only
# when the failed candidate is an ancestor with identical database/GIS inputs.
RETRY_FROM="${CMOS_DEPLOY_RETRY_FROM:-}"
RETRY_BACKUP="${CMOS_DEPLOY_RETRY_BACKUP:-}"
SENSITIVE_PATHS=(
  deploy/postgis/init
  deploy/gis
  schemas
)
if [[ "${CMOS_FORCE_FULL_BACKUP:-false}" != "true" && -n "$RETRY_FROM" && -n "$RETRY_BACKUP" ]]; then
  if [[ "$RETRY_FROM" =~ ^[0-9a-f]{40}$ ]] &&
     git cat-file -e "$RETRY_FROM^{commit}" 2>/dev/null &&
     git merge-base --is-ancestor "$RETRY_FROM" "$TARGET" &&
     git diff --quiet "$RETRY_FROM" "$TARGET" -- "${SENSITIVE_PATHS[@]}"; then
    echo "BACKUP GATE: checking explicit failed-release recovery point"
    if verified="$(BACKUP_MAX_AGE_HOURS="$MAX_AGE_HOURS" bash deploy/postgis/verify-backup.sh)"; then
      printf '%s\n' "$verified"
      verified_file="$(printf '%s\n' "$verified" | sed -n 's/^file=//p' | sed -n '1p')"
      if [[ -n "$verified_file" && "$RETRY_BACKUP" == "$verified_file" ]]; then
        echo "BACKUP GATE: PASS — reusing named, checksum-verified retry backup; database/GIS inputs unchanged"
        exit 0
      fi
      echo "BACKUP GATE: named retry archive is not the latest verified recovery point"
    else
      printf '%s\n' "$verified"
      echo "BACKUP GATE: retry recovery point did not validate"
    fi
  else
    echo "BACKUP GATE: retry ancestry or database/GIS comparison did not pass"
  fi
  echo "BACKUP GATE: falling back to normal recovery-point requirements"
fi

force=false
reason="routine application release"

if [[ "${CMOS_FORCE_FULL_BACKUP:-false}" == "true" ]]; then
  force=true
  reason="CMOS_FORCE_FULL_BACKUP requested"
elif [[ -z "$PREVIOUS" ]] || ! git cat-file -e "$PREVIOUS^{commit}" 2>/dev/null; then
  force=true
  reason="no prior release marker available"
elif ! git diff --quiet "$PREVIOUS" "$TARGET" -- "${SENSITIVE_PATHS[@]}"; then
  force=true
  reason="database/GIS-sensitive files changed"
fi

if [[ "$force" == "true" ]]; then
  echo "BACKUP GATE: fresh full logical backup required — $reason"
  bash deploy/postgis/backup.sh
  exit 0
fi

# Every routine deploy gets a small control-plane recovery snapshot. Isolated
# gate tests and recovery copies may not carry that helper, so absence is not fatal.
if [[ -f deploy/postgis/release-snapshot.sh ]]; then
  bash deploy/postgis/release-snapshot.sh "$PREVIOUS" "$TARGET"
else
  echo "BACKUP GATE: lightweight release snapshot helper unavailable — continuing with full recovery-point policy"
fi

echo "BACKUP GATE: routine release — checking for nightly validated full backup <= ${MAX_AGE_HOURS}h"
if BACKUP_MAX_AGE_HOURS="$MAX_AGE_HOURS" bash deploy/postgis/verify-backup.sh; then
  echo "BACKUP GATE: PASS — reusing recent validated recovery point; routine snapshot avoids a new multi-GB dump"
else
  echo "BACKUP GATE: scheduled full backup is stale/missing — creating one now for safety"
  bash deploy/postgis/backup.sh
fi
