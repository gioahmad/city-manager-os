#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
PREVIOUS="${1:-}"
TARGET="${2:-$(git -C "$REPO" rev-parse HEAD)}"
ROOT="${CMOS_RELEASE_SNAPSHOT_DIR:-/var/backups/city-manager-os/release-snapshots}"
KEEP="${CMOS_RELEASE_SNAPSHOT_KEEP:-10}"
DAYS="${CMOS_RELEASE_SNAPSHOT_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$REPO"
mkdir -p "$ROOT"
OUT="$(mktemp -d "$ROOT/.${STAMP}.XXXXXX")"
FINAL="$ROOT/${OUT##*/.}"
trap 'rm -rf -- "$OUT"' EXIT

ENV="$REPO/deploy/postgis/.env"
[[ -f "$ENV" ]] || { echo "ERROR: $ENV not found"; exit 1; }
set -a
source "$ENV"
set +a

printf 'created_at=%s\n' "$(date --iso-8601=seconds)" > "$OUT/release.txt"
printf 'previous=%s\n' "$PREVIOUS" >> "$OUT/release.txt"
printf 'target=%s\n' "$TARGET" >> "$OUT/release.txt"
git status --short > "$OUT/git-status.txt"
if [[ -n "$PREVIOUS" ]] && git cat-file -e "$PREVIOUS^{commit}" 2>/dev/null; then
  git diff --name-status "$PREVIOUS" "$TARGET" > "$OUT/changed-files.txt"
else
  printf 'No usable previous release marker.\n' > "$OUT/changed-files.txt"
fi

# Preserve the entire current schema and all application data, including new
# contacts/workspace tables. Only bulk GIS reference/staging rows are omitted.
# This is a scoped logical snapshot, not a block-level incremental backup.
EXCLUDED="$(docker exec -i citymanager-postgis sh -lc 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT format('%I.%I', schemaname, tablename)
FROM pg_tables
WHERE schemaname='public'
  AND (
    (left(tablename,4)='gis_' AND tablename NOT IN ('gis_dataset_versions','gis_refresh_runs'))
    OR tablename ~ '^(stg|old|backup)_(gis|nj|nyc)_'
  )
ORDER BY tablename;
SQL
)"

ARGS=()
while IFS= read -r table; do
  [[ -n "$table" ]] && ARGS+=(--exclude-table-data-and-children="$table")
done <<< "$EXCLUDED"

docker exec citymanager-postgis pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  "${ARGS[@]}" > "$OUT/application.dump"
[[ -s "$OUT/application.dump" ]]
docker exec -i citymanager-postgis pg_restore --list < "$OUT/application.dump" >/dev/null
(cd "$OUT" && sha256sum application.dump > application.dump.sha256 && sha256sum -c application.dump.sha256 >/dev/null)
printf '%s\n' "$EXCLUDED" > "$OUT/excluded-data-tables.txt"
printf 'scope=all_schema_and_non_bulk_gis_data\n' >> "$OUT/release.txt"
chmod -R go-rwx "$OUT"
mv "$OUT" "$FINAL"
trap - EXIT

# Retention: age first, then cap the number of routine snapshots.
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime "+$DAYS" -print -exec rm -rf -- {} +
if [[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP > 0 )); then
  mapfile -t stale < <(
    find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' -printf '%T@ %p\n' |
      sort -nr | awk -v keep="$KEEP" 'NR>keep {sub(/^[^ ]+ /,""); print}'
  )
  for path in "${stale[@]}"; do
    echo "Removing old routine release snapshot: $path"
    rm -rf -- "$path"
  done
fi

echo "APPLICATION RELEASE SNAPSHOT: PASS $FINAL"
echo "Schema and application data protected; omitted GIS rows remain in the scheduled full backups."
