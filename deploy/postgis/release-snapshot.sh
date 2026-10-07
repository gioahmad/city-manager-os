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
OUT="$ROOT/$STAMP"

cd "$REPO"
mkdir -p "$OUT"

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

# A routine release snapshot is intentionally small. It protects control-plane
# state that is most likely to be edited between full database backups. It is
# not represented as a complete or incremental PostgreSQL backup.
mapfile -t TABLES < <(
  docker exec -i citymanager-postgis sh -lc 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT tablename
FROM pg_tables
WHERE schemaname='public'
  AND tablename = ANY(ARRAY[
    'watch_items','subscribers','watch_item_recipients',
    'workspace_config','workspace_entities','workspace_relationships','workspace_dates',
    'workspace_personal_tasks','brain_notes',
    'issues','issue_checklist_items','issue_updates',
    'map_layers','source_health'
  ])
ORDER BY tablename;
SQL
)

ARGS=()
for table in "${TABLES[@]}"; do
  [[ -n "$table" ]] && ARGS+=(--table="public.$table")
done

if (( ${#ARGS[@]} > 0 )); then
  docker exec citymanager-postgis pg_dump     -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --data-only     "${ARGS[@]}" > "$OUT/control-plane.dump"
  [[ -s "$OUT/control-plane.dump" ]]
  docker exec -i citymanager-postgis pg_restore --list < "$OUT/control-plane.dump" >/dev/null
  (cd "$OUT" && sha256sum control-plane.dump > control-plane.dump.sha256)
fi

printf '%s\n' "${TABLES[@]}" > "$OUT/tables.txt"
chmod -R go-rwx "$OUT"

# Retention: age first, then cap the number of routine snapshots.
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$DAYS" -print -exec rm -rf -- {} +
if [[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP > 0 )); then
  mapfile -t stale < <(
    find "$ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' |
      sort -nr | awk -v keep="$KEEP" 'NR>keep {sub(/^[^ ]+ /,""); print}'
  )
  for path in "${stale[@]}"; do
    echo "Removing old routine release snapshot: $path"
    rm -rf -- "$path"
  done
fi

echo "ROUTINE RELEASE SNAPSHOT: PASS $OUT"
echo "This is a small control-plane recovery snapshot; full PostgreSQL protection remains deploy/postgis/backup.sh."
