#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $SCRIPT_DIR"
  exit 1
fi

set -a
source .env
set +a

mkdir -p backups
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="backups/citymanager_${STAMP}.dump"

echo "Creating backup: $OUT"

docker exec citymanager-postgis \
  pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -Fc > "$OUT"

echo "Backup complete: $OUT"

# Keep the most recent 14 days of database backups.
find backups -type f -name 'citymanager_*.dump' -mtime +14 -delete

echo "Retention cleanup complete."
