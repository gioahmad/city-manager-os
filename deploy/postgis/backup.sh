#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

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

docker exec citymanager-postgis \
  pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -Fc > "$TMP"

[[ -s "$TMP" ]] || {
  echo "ERROR: backup file is empty"
  exit 1
}

printf 'Validating archive with pg_restore --list...\n'
docker exec -i citymanager-postgis \
  pg_restore --list \
  < "$TMP" \
  >/dev/null

mv "$TMP" "$OUT"

(
  cd "$BACKUP_DIR"
  sha256sum "$NAME" > "$NAME.sha256"
  sha256sum -c "$NAME.sha256" >/dev/null
)

OFFBOX_STATUS="NOT_CONFIGURED"

if [[ -n "$OFFBOX_DIR" ]]; then
  [[ -d "$OFFBOX_DIR" ]] || {
    echo "ERROR: BACKUP_OFFBOX_DIR does not exist: $OFFBOX_DIR"
    exit 1
  }
  install -m 600 "$OUT" "$OFFBOX_DIR/$NAME"
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
  rsync -a --chmod=F600 "$OUT" "$SHA" "${OFFBOX_TARGET%/}/"
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

find "$BACKUP_DIR" -type f -name 'citymanager_*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'citymanager_*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete

printf 'Backup complete and validated: %s\n' "$OUT"
printf 'Off-box status: %s\n' "$OFFBOX_STATUS"
printf 'Retention cleanup complete (%s days).\n' "$RETENTION_DAYS"
