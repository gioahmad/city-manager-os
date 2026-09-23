#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
EXPECTED_TARGET="${1:-}"
MIGRATION="$REPO/deploy/postgis/init/035_contact_directory.sql"

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "CONTACT DIRECTORY INSTALL FAILED rc=${rc} line=${LINENO}"; exit "$rc"' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ -s "$MIGRATION" ]] || fail "contact migration is missing"
[[ "$(docker inspect citymanager-postgis --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
  || fail "citymanager-postgis is not running"

"$REPO/deploy/postgis/backup.sh"

docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT
  to_regclass('public.contacts') IS NOT NULL
  AND to_regclass('public.issue_contacts') IS NOT NULL
  AND to_regclass('public.contact_activity') IS NOT NULL
  AND EXISTS(
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='subscribers' AND column_name='contact_id'
  )
  AND NOT EXISTS(SELECT 1 FROM subscribers WHERE contact_id IS NULL)
  AND has_table_privilege('citymanager_app','contacts','SELECT,INSERT,UPDATE,DELETE');
SQL
)"
[[ "$STATE" == t ]] || fail "contact directory readiness contract failed"

log "CONTACT DIRECTORY INSTALL: PASS"
