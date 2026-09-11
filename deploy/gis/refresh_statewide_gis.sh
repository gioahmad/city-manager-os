#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
MODE="${GIS_REFRESH_MODE:-full}"
RUN_ID="NJ-$(date '+%Y%m%d%H%M%S')"
MANIFEST_DIR="/opt/citymanager-data/gis/manifests"
SOURCE_MANIFEST="${MANIFEST_DIR}/source-${RUN_ID}.json"
METADATA_DIR="/opt/citymanager-data/gis/source-metadata"
ADDRESS_ARCHIVE="/opt/citymanager-data/gis/incoming/Addr_NG911.gdb.zip"
PARCEL_ARCHIVE="/opt/citymanager-data/gis/incoming/parcels_MOD4_Statewide.gdb.zip"
LOCK_FILE="/var/lock/cmos-nj-statewide-refresh.lock"
NOTIFY="$REPO/deploy/gis/notify_gis_refresh.sh"
FAILED=1

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
db(){ docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }

record(){
  local status="$1" phase="$2" error="${3:-}" completed="${4:-false}"
  docker exec -i \
    -e REFRESH_RUN_ID="$RUN_ID" -e REFRESH_MODE="$MODE" -e REFRESH_STATUS="$status" \
    -e REFRESH_PHASE="$phase" -e REFRESH_ERROR="$error" -e REFRESH_COMPLETED="$completed" \
    -e REFRESH_REPOSITORY="${HEAD_SHA:-}" -e REFRESH_MANIFEST="${SOURCE_JSON:-{}}" \
    citymanager-postgis sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v run_id="$REFRESH_RUN_ID" -v mode="$REFRESH_MODE" -v status="$REFRESH_STATUS" \
  -v phase="$REFRESH_PHASE" -v error="$REFRESH_ERROR" -v completed="$REFRESH_COMPLETED" \
  -v repository="$REFRESH_REPOSITORY" -v manifest="$REFRESH_MANIFEST"
' <<'SQL' >/dev/null
INSERT INTO gis_refresh_runs(run_id,scope,mode,status,phase,source_manifest,repository_sha,error_message)
VALUES (:'run_id','NJ_STATEWIDE',:'mode',:'status',:'phase',:'manifest'::jsonb,nullif(:'repository',''),nullif(:'error',''))
ON CONFLICT(run_id) DO UPDATE
SET status=excluded.status,phase=excluded.phase,source_manifest=excluded.source_manifest,
    repository_sha=excluded.repository_sha,error_message=excluded.error_message,
    updated_at=now(),completed_at=CASE WHEN :'completed'::boolean THEN now() ELSE NULL END;
SQL
}

cleanup_failure(){
  local rc=$?
  trap - ERR EXIT
  if (( FAILED )); then
    local message="NJ statewide GIS refresh failed in phase ${PHASE:-startup} with exit code ${rc}. Production GIS was retained unless atomic promotion completed."
    record FAILED "${PHASE:-startup}" "$message" true 2>/dev/null || true
    "$NOTIFY" FAILURE "$message" "$RUN_ID" 2>/dev/null || true
    log "$message"
  fi
  exit "$rc"
}
trap cleanup_failure ERR EXIT

[[ "$MODE" == "full" || "$MODE" == "validate" ]] || fail "GIS_REFRESH_MODE must be full or validate"
for cmd in docker flock git python3 sha256sum systemctl; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another statewide GIS lifecycle run is active"
install -d -m 0750 "$MANIFEST_DIR"

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"
HEAD_SHA="$(git rev-parse HEAD)"
PHASE="VALIDATING"
record RUNNING "$PHASE"

if [[ "$MODE" == validate ]]; then
  python3 deploy/gis/statewide_bulk_refresh.py probe --manifest "$SOURCE_MANIFEST"
  SOURCE_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])),separators=(",",":")))' "$SOURCE_MANIFEST")"
  CMOS_NJ_ADDRESS_SHA256="$(sha256sum "$ADDRESS_ARCHIVE" | awk '{print $1}')" \
  CMOS_NJ_PARCEL_SHA256="$(sha256sum "$PARCEL_ARCHIVE" | awk '{print $1}')" \
    deploy/gis/import_statewide_gis.sh validate
  record SUCCESS COMPLETE "" true
  FAILED=0
  log "NJ STATEWIDE GIS REFRESH VALIDATION PASSED"
  exit 0
fi

PHASE="SYNCING"
record RUNNING "$PHASE"
python3 deploy/gis/statewide_bulk_refresh.py sync --bootstrap-existing --manifest "$SOURCE_MANIFEST"
SOURCE_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])),separators=(",",":")))' "$SOURCE_MANIFEST")"
record RUNNING "$PHASE"
mapfile -t SOURCE_VALUES < <(python3 - "$SOURCE_MANIFEST" <<'PY'
import json, sys
data=json.load(open(sys.argv[1]))
sources={item['name']:item for item in data['sources']}
for name in ('addresses','parcels'):
    item=sources[name]
    print(item['sha256'])
    print('1' if item.get('changed') else '0')
PY
)
ADDRESS_SHA="${SOURCE_VALUES[0]}"; ADDRESS_CHANGED="${SOURCE_VALUES[1]}"
PARCEL_SHA="${SOURCE_VALUES[2]}"; PARCEL_CHANGED="${SOURCE_VALUES[3]}"

if [[ "$ADDRESS_CHANGED" == 0 && "$PARCEL_CHANGED" == 0 ]]; then
  record UNCHANGED COMPLETE "" true
  FAILED=0
  "$NOTIFY" SUCCESS "NJ statewide GIS source check completed. Official parcel and address revisions are unchanged; production data was reused." "$RUN_ID" || log "WARNING: completion notification failed"
  log "NJ STATEWIDE GIS REFRESH UNCHANGED"
  exit 0
fi

PHASE="BACKUP"
record RUNNING "$PHASE"
deploy/postgis/backup.sh
deploy/postgis/verify-backup.sh

stage_sources(){
  CMOS_NJ_ADDRESS_ARCHIVE="$1" CMOS_NJ_ADDRESS_SHA256="$2" \
  CMOS_NJ_PARCEL_ARCHIVE="$3" CMOS_NJ_PARCEL_SHA256="$4" \
    deploy/gis/import_statewide_gis.sh stage
}

activate_retained_source(){
  local archive="$1" metadata="${METADATA_DIR}/$(basename "$1").metadata.json"
  local rejected="${archive}.rejected-${RUN_ID}"
  mv "$archive" "$rejected"
  ln "${archive}.previous" "$archive"
  if [[ -f "${metadata}.previous" ]]; then
    mv -f "${metadata}.previous" "$metadata"
  else
    python3 - "$metadata" "$archive" <<'PY'
import json, pathlib, sys
path, archive = map(pathlib.Path, sys.argv[1:])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
    "content_length": archive.stat().st_size,
    "etag": "RETAINED_PRIOR_RETRY_REQUIRED",
    "last_modified": "RETAINED_PRIOR_RETRY_REQUIRED",
}, sort_keys=True) + "\n")
PY
  fi
  log "Retained prior source activated; rejected revision saved as ${rejected}"
}

PHASE="STAGING"
record RUNNING "$PHASE"
set +e
stage_sources "$ADDRESS_ARCHIVE" "$ADDRESS_SHA" "$PARCEL_ARCHIVE" "$PARCEL_SHA"
STAGE_RC=$?
set -e
if (( STAGE_RC != 0 )); then
  FALLBACK_ADDRESS="$ADDRESS_ARCHIVE"; FALLBACK_ADDRESS_SHA="$ADDRESS_SHA"
  FALLBACK_PARCEL="$PARCEL_ARCHIVE"; FALLBACK_PARCEL_SHA="$PARCEL_SHA"
  if [[ "$ADDRESS_CHANGED" == 1 && -f "${ADDRESS_ARCHIVE}.previous" ]]; then
    FALLBACK_ADDRESS="${ADDRESS_ARCHIVE}.previous"; FALLBACK_ADDRESS_SHA="$(sha256sum "$FALLBACK_ADDRESS" | awk '{print $1}')"
  fi
  if [[ "$PARCEL_CHANGED" == 1 && -f "${PARCEL_ARCHIVE}.previous" ]]; then
    FALLBACK_PARCEL="${PARCEL_ARCHIVE}.previous"; FALLBACK_PARCEL_SHA="$(sha256sum "$FALLBACK_PARCEL" | awk '{print $1}')"
  fi
  [[ "$FALLBACK_ADDRESS" != "$ADDRESS_ARCHIVE" || "$FALLBACK_PARCEL" != "$PARCEL_ARCHIVE" ]] \
    || fail "new source staging failed and no retained prior source is available"
  log "New source staging failed; retrying once with retained prior archive revision"
  stage_sources "$FALLBACK_ADDRESS" "$FALLBACK_ADDRESS_SHA" "$FALLBACK_PARCEL" "$FALLBACK_PARCEL_SHA"
  if [[ "$FALLBACK_ADDRESS" != "$ADDRESS_ARCHIVE" ]]; then activate_retained_source "$ADDRESS_ARCHIVE"; fi
  if [[ "$FALLBACK_PARCEL" != "$PARCEL_ARCHIVE" ]]; then activate_retained_source "$PARCEL_ARCHIVE"; fi
  ADDRESS_SHA="$FALLBACK_ADDRESS_SHA"
  PARCEL_SHA="$FALLBACK_PARCEL_SHA"
fi

PHASE="PROMOTING"
record RUNNING "$PHASE"
CMOS_NJ_ADDRESS_ARCHIVE="$ADDRESS_ARCHIVE" CMOS_NJ_ADDRESS_SHA256="$ADDRESS_SHA" \
CMOS_NJ_PARCEL_ARCHIVE="$PARCEL_ARCHIVE" CMOS_NJ_PARCEL_SHA256="$PARCEL_SHA" \
  deploy/gis/import_statewide_gis.sh promote

PHASE="CLEANUP"
record RUNNING "$PHASE"
db <<'SQL'
DROP TABLE IF EXISTS stg_nj_parcels,stg_nj_addresses,stg_nj_parcel_blocks,stg_nj_road_aliases,stg_nj_landmark_aliases;
DO $$
DECLARE prefix text; keep_name text; old_name text;
BEGIN
  FOREACH prefix IN ARRAY ARRAY['gis_parcels_backup_','gis_addresses_backup_','gis_parcel_blocks_backup_','gis_road_aliases_backup_','gis_landmark_aliases_backup_'] LOOP
    SELECT c.relname INTO keep_name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind='r' AND left(c.relname,length(prefix))=prefix ORDER BY c.relname DESC LIMIT 1;
    FOR old_name IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relkind='r' AND left(c.relname,length(prefix))=prefix AND c.relname<>keep_name
    LOOP EXECUTE format('DROP TABLE public.%I',old_name); END LOOP;
  END LOOP;
END $$;
SQL
find /opt/citymanager-data/gis/incoming -maxdepth 1 -type f -name '*.rejected-NJ-*' -delete

record SUCCESS COMPLETE "" true
FAILED=0
"$NOTIFY" SUCCESS "NJ statewide GIS refresh completed and production was atomically promoted from validated local archives." "$RUN_ID" || log "WARNING: completion notification failed"
log "NJ STATEWIDE GIS REFRESH PASSED"
