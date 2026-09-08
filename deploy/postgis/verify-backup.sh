#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[[ -f .env ]] || {
  echo "ERROR: .env not found"
  exit 1
}

set -a
source .env
set +a

BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/backups}"
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-36}"

# Do not use `head -n1` in this pipe. With `set -o pipefail`, head may
# close the pipe after the first row and cause upstream sort to exit 141
# (SIGPIPE) when the backup directory contains enough files.
LATEST="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'citymanager_*.dump' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1p' | cut -d' ' -f2-)"
[[ -n "$LATEST" && -s "$LATEST" ]] || {
  echo "ERROR: no backup archive found"
  exit 1
}

SHA="$LATEST.sha256"
[[ -s "$SHA" ]] || {
  echo "ERROR: checksum file missing for $LATEST"
  exit 1
}

(
  cd "$BACKUP_DIR"
  sha256sum -c "$(basename "$SHA")" >/dev/null
)

docker exec -i citymanager-postgis \
  pg_restore --list \
  < "$LATEST" \
  >/dev/null

AGE_SECONDS=$(( $(date +%s) - $(stat -c %Y "$LATEST") ))
MAX_SECONDS=$(( MAX_AGE_HOURS * 3600 ))

if (( AGE_SECONDS > MAX_SECONDS )); then
  echo "ERROR: latest backup is older than ${MAX_AGE_HOURS}h"
  exit 1
fi

printf 'BACKUP VERIFY: PASS\n'
printf 'file=%s\n' "$LATEST"
printf 'age_seconds=%s\n' "$AGE_SECONDS"
printf 'sha256=%s\n' "$(awk '{print $1}' "$SHA")"

if [[ -f "$BACKUP_DIR/LATEST_VALIDATED" ]]; then
  cat "$BACKUP_DIR/LATEST_VALIDATED"
fi
