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
BACKUP="${1:-}"

if [[ -z "$BACKUP" ]]; then
  BACKUP="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'citymanager_*.dump' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
fi

[[ -n "$BACKUP" && -s "$BACKUP" ]] || {
  echo "ERROR: backup archive not found"
  exit 1
}

STAMP="$(date +%Y%m%d%H%M%S)"
SCRATCH_DB="cmos_restore_${STAMP}_$$"
CREATED=0

cleanup(){
  set +e
  if (( CREATED == 1 )); then
    docker exec citymanager-postgis \
      dropdb --if-exists --force \
      -U "$POSTGRES_USER" \
      "$SCRATCH_DB" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

printf 'Creating scratch database: %s\n' "$SCRATCH_DB"

docker exec citymanager-postgis \
  createdb \
  -U "$POSTGRES_USER" \
  -T template0 \
  "$SCRATCH_DB"
CREATED=1

printf 'Restoring archive into scratch database...\n'
docker exec -i citymanager-postgis \
  pg_restore \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  -U "$POSTGRES_USER" \
  -d "$SCRATCH_DB" \
  < "$BACKUP"

CHECK="$(docker exec citymanager-postgis \
  psql -X -qAt \
  -U "$POSTGRES_USER" \
  -d "$SCRATCH_DB" \
  -c "
SELECT CASE WHEN
  to_regclass('public.issues') IS NOT NULL
  AND to_regclass('public.alerts') IS NOT NULL
  AND to_regclass('public.integrations') IS NOT NULL
  AND to_regclass('public.map_layers') IS NOT NULL
  AND to_regclass('public.gis_parcels') IS NOT NULL
  AND to_regclass('public.operations_routines') IS NOT NULL
THEN 'PASS' ELSE 'FAIL' END;
")"

[[ "$CHECK" == "PASS" ]] || {
  echo "ERROR: required restored tables are missing"
  exit 1
}

COUNTS="$(docker exec citymanager-postgis \
  psql -X -qAt \
  -U "$POSTGRES_USER" \
  -d "$SCRATCH_DB" \
  -c "
SELECT
  (SELECT count(*) FROM issues)::text || '|' ||
  (SELECT count(*) FROM integrations)::text || '|' ||
  (SELECT count(*) FROM map_layers)::text || '|' ||
  (SELECT count(*) FROM gis_parcels)::text;
")"

printf 'SCRATCH RESTORE: PASS\n'
printf 'backup=%s\n' "$BACKUP"
printf 'database=%s\n' "$SCRATCH_DB"
printf 'counts_issues_integrations_layers_parcels=%s\n' "$COUNTS"
printf 'Scratch database will now be dropped.\n'
