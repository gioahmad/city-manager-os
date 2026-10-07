#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/../progress.sh"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $SCRIPT_DIR"
  exit 1
fi

set -a
source .env
set +a

BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/backups}"
OFFBOX_DIR="${BACKUP_OFFBOX_DIR:-}"
OFFBOX_TARGET="${BACKUP_OFFBOX_TARGET:-}"
REQUIRE_OFFBOX="${BACKUP_REQUIRE_OFFBOX:-false}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-10}"
MAX_LOCAL_COUNT="${BACKUP_MAX_LOCAL_COUNT:-2}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
NAME="citymanager_${STAMP}.dump"
OUT="$BACKUP_DIR/$NAME"
TMP="$OUT.partial"
SHA="$OUT.sha256"
MARKER="$BACKUP_DIR/LATEST_VALIDATED"

cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT

printf 'Creating backup: %s\n' "$OUT"

progress_step 0 5 "Creating database archive"
run_with_progress "Database dump" "$TMP" docker exec citymanager-postgis \
  pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -Fc > "$TMP"

[[ -s "$TMP" ]] || {
  echo "ERROR: backup file is empty"
  exit 1
}

progress_step 1 5 "Validating archive"
printf 'Validating archive with pg_restore --list...\n'
run_with_progress "Archive validation" "" docker exec -i citymanager-postgis \
  pg_restore --list \
  < "$TMP" \
  >/dev/null

mv "$TMP" "$OUT"

progress_step 2 5 "Calculating and checking checksum"
(
  cd "$BACKUP_DIR"
  sha256sum "$NAME" > "$NAME.sha256"
  sha256sum -c "$NAME.sha256" >/dev/null
)

progress_step 3 5 "Off-box copy (if configured)"
OFFBOX_STATUS="NOT_CONFIGURED"

if [[ -n "$OFFBOX_DIR" ]]; then
  [[ -d "$OFFBOX_DIR" ]] || {
    echo "ERROR: BACKUP_OFFBOX_DIR does not exist: $OFFBOX_DIR"
    exit 1
  }
  run_with_progress "Off-box archive copy" "" install -m 600 "$OUT" "$OFFBOX_DIR/$NAME"
  install -m 600 "$SHA" "$OFFBOX_DIR/$NAME.sha256"
  (
    cd "$OFFBOX_DIR"
    sha256sum -c "$NAME.sha256" >/dev/null
  )
  OFFBOX_STATUS="COPIED_DIR:$OFFBOX_DIR"
elif [[ -n "$OFFBOX_TARGET" ]]; then
  command -v rsync >/dev/null || {
    echo "ERROR: rsync is required for BACKUP_OFFBOX_TARGET"
    exit 1
  }
  run_with_progress "Off-box transfer" "" rsync -a --chmod=F600 "$OUT" "$SHA" "${OFFBOX_TARGET%/}/"
  OFFBOX_STATUS="COPIED_RSYNC:$OFFBOX_TARGET"
elif [[ "$REQUIRE_OFFBOX" == "true" ]]; then
  echo "ERROR: off-box backup is required but no target is configured"
  exit 1
fi

{
  printf 'validated_at=%s\n' "$(date --iso-8601=seconds)"
  printf 'file=%s\n' "$OUT"
  printf 'sha256=%s\n' "$(awk '{print $1}' "$SHA")"
  printf 'offbox=%s\n' "$OFFBOX_STATUS"
} > "$MARKER.tmp"
mv "$MARKER.tmp" "$MARKER"
chmod 600 "$MARKER"

progress_step 4 5 "Retention cleanup"
find "$BACKUP_DIR" -type f -name 'citymanager_*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'citymanager_*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete

# Full GIS-enabled dumps are large. Repeated deploy/retry runs can otherwise
# create many multi-GB backups on the same day. Keep only the newest validated
# local archives after age retention. Longer retention belongs off-box.
if [[ "$MAX_LOCAL_COUNT" =~ ^[0-9]+$ ]] && (( MAX_LOCAL_COUNT > 0 )); then
  mapfile -t stale_local < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'citymanager_*.dump' -printf '%T@ %p\n' 2>/dev/null |
      sort -nr |
      awk -v keep="$MAX_LOCAL_COUNT" 'NR>keep {sub(/^[^ ]+ /,""); print}'
  )
  for old_dump in "${stale_local[@]}"; do
    [[ "$old_dump" == "$OUT" ]] && continue
    rm -f -- "$old_dump" "$old_dump.sha256"
  done
fi

progress_step 5 5 "Backup complete and validated"
printf 'Backup complete and validated: %s\n' "$OUT"
printf 'Off-box status: %s\n' "$OFFBOX_STATUS"
printf 'Retention cleanup complete (%s days; newest %s local full backups retained).\n' "$RETENTION_DAYS" "$MAX_LOCAL_COUNT"
