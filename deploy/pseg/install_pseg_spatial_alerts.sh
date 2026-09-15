#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
EXPECTED_TARGET="${1:-}"
MIGRATION="$REPO/deploy/postgis/init/033_pseg_spatial_alerts.sql"

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "#60 PSEG DATABASE INSTALL FAILED rc=${rc} line=${LINENO}"; exit "$rc"' ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ -s "$MIGRATION" ]] || fail "PSEG migration is missing"
[[ "$(docker inspect citymanager-postgis --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
  || fail "citymanager-postgis is not running"

log "Applying additive PSEG settings and durable source-state migration"
docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

STATE="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT
  to_regclass('public.pseg_alert_settings') IS NOT NULL
  AND to_regclass('public.pseg_outage_state') IS NOT NULL
  AND to_regclass('public.pseg_alert_status') IS NOT NULL
  AND EXISTS(
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='pseg_outage_state'
      AND column_name='material_baseline_customers_out'
  )
  AND (SELECT count(*)=1 FROM pseg_alert_settings WHERE settings_key='DEFAULT')
  AND (SELECT minimum_customers BETWEEN 1 AND 10000000 AND poll_minutes=15
       FROM pseg_alert_settings WHERE settings_key='DEFAULT')
  AND has_table_privilege('citymanager_app','pseg_alert_settings','SELECT,UPDATE')
  AND has_table_privilege('citymanager_app','pseg_outage_state','SELECT,INSERT,UPDATE,DELETE');
SQL
)"
[[ "$STATE" == t ]] || fail "PSEG database readiness contract failed"

log "#60 PSEG DATABASE INSTALL: PASS"
log "No source poll or notification was triggered by this installer"
